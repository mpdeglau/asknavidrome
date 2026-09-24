from datetime import datetime
from flask import Flask, render_template, request
from typing import Union
import hmac
import logging
from multiprocessing import Process
from multiprocessing.managers import BaseManager
import os
import random
import sys

from werkzeug.exceptions import BadRequest, MethodNotAllowed, NotFound

from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.dispatch_components import AbstractRequestHandler, AbstractRequestInterceptor, AbstractResponseInterceptor
from ask_sdk_core.utils import is_request_type, is_intent_name, get_slot_value_v2, get_intent_name, get_request_type
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_model import Response
from ask_sdk_model.ui import StandardCard
from ask_sdk_core.dispatch_components import AbstractExceptionHandler
from flask_ask_sdk.skill_adapter import SkillAdapter

import asknavidrome.subsonic_api as api
import asknavidrome.media_queue as queue
import asknavidrome.controller as controller


def resolved_slot_value(slot):
    """Return a slot's entity-resolved canonical value (e.g. matched via a
    custom slot type synonym) when available, falling back to the raw
    spoken/transcribed text otherwise."""
    if slot.resolutions and slot.resolutions.resolutions_per_authority:
        for authority in slot.resolutions.resolutions_per_authority:
            if authority.status.code == 'ER_SUCCESS_MATCH' and authority.values:
                return authority.values[0].value.name
    return slot.value


# Create web service
app = Flask(__name__)

# Create skill object
sb = SkillBuilder()

# Setup Logging
logger = logging.getLogger()  # Create logger
level = logging.getLevelName('DEBUG')
logger.setLevel(level)  # Set logger log level

log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(level)
handler.setFormatter(log_formatter)

logger.addHandler(handler)

#
# Get service configuration
#

logger.info('AskNavidrome 0.10!')
logger.debug('Getting configuration from the environment...')

try:
    if 'NAVI_SKILL_ID' in os.environ:
        # Set skill ID, this is available on the Alexa Developer Console
        # if this is not set the web service will respond to any skill.
        sb.skill_id = os.getenv('NAVI_SKILL_ID')

        logger.info(f'Skill ID set to: {sb.skill_id}')

    else:
        raise NameError
except NameError as err:
    logger.error(f'The Alexa skill ID was not found! {err}')
    raise

try:
    if 'NAVI_SONG_COUNT' in os.environ:
        min_song_count = os.getenv('NAVI_SONG_COUNT')

        logger.info(f'Minimum song count is set to: {min_song_count}')

    else:
        raise NameError
except NameError as err:
    logger.error(f'The minimum song count was not found! {err}')
    raise

try:
    if 'NAVI_URL' in os.environ:
        navidrome_url = os.getenv('NAVI_URL')

        logger.info(f'The URL for Navidrome is set to: {navidrome_url}')

    else:
        raise NameError
except NameError as err:
    logger.error(f'The URL of the Navidrome server was not found! {err}')
    raise

try:
    if 'NAVI_USER' in os.environ:
        navidrome_user = os.getenv('NAVI_USER')

        logger.info(f'The Navidrome user name is set to: {navidrome_user}')

    else:
        raise NameError
except NameError as err:
    logger.error(f'The Navidrome user name was not found! {err}')
    raise

try:
    if 'NAVI_PASS' in os.environ:
        navidrome_passwd = os.getenv('NAVI_PASS')

        logger.info('The Navidrome password is set')

    else:
        raise NameError
except NameError as err:
    logger.error(f'The Navidrome password was not found! {err}')
    raise

try:
    if 'NAVI_PORT' in os.environ:
        navidrome_port = os.getenv('NAVI_PORT')

        logger.info(f'The Navidrome port is set to: {navidrome_port}')

    else:
        raise NameError
except NameError as err:
    logger.error(f'The Navidrome port was not found! {err}')
    raise

try:
    if 'NAVI_API_PATH' in os.environ:
        navidrome_api_location = os.getenv('NAVI_API_PATH')

        logger.info(f'The Navidrome API path is set to: {navidrome_api_location}')

    else:
        raise NameError
except NameError as err:
    logger.error(f'The Navidrome API path was not found! {err}')
    raise

try:
    if 'NAVI_API_VER' in os.environ:
        navidrome_api_version = os.getenv('NAVI_API_VER')

        logger.info(f'The Navidrome API version is set to: {navidrome_api_version}')

    else:
        raise NameError
except NameError as err:
    logger.error(f'The Navidrome API version was not found! {err}')
    raise

logger.debug('Configuration has been successfully loaded')

# Set log level based on config value
if 'NAVI_DEBUG' in os.environ:
    navidrome_log_level = int(os.getenv('NAVI_DEBUG'))

    if navidrome_log_level == 0:
        # Warnings and higher
        logger.setLevel(logging.WARNING)
        logger.warning('Log level set to WARNING')

    elif navidrome_log_level == 1:
        # Info messages and higher
        logger.setLevel(logging.INFO)
        logger.info('Log level set to INFO')

    elif navidrome_log_level == 2:
        # Debug with request and response interceptors
        logger.setLevel(logging.DEBUG)
        logger.debug('Log level set to DEBUG')

    elif navidrome_log_level == 3:
        # Debug with request / response interceptors and Web GUI
        logger.setLevel(logging.DEBUG)
        logger.debug('Log level set to DEBUG')

    else:
        # Invalid value provided - set to WARNING
        navidrome_log_level = 0
        logger.setLevel(logging.WARNING)
        logger.warning('Log level set to WARNING')

# Playback state is kept per Alexa device (keyed by device ID) rather than
# in a single shared queue, so that two Echo devices can each play something
# different at the same time instead of one hijacking the other's queue.
BaseManager.register('MediaQueue', queue.MediaQueue)
manager = BaseManager()
manager.start()

# device_id -> MediaQueue (a manager proxy, shareable with queue_worker_thread's
# background Process)
play_queues = {}

# device_id -> the additional Process used to populate large playlists in the
# background. Keyed per device to avoid one device's playlist load cancelling
# another's.
background_processes = {}

# device_id -> human-readable description of what's currently loaded into
# that device's queue (e.g. 'the playlist Crossover', 'the album X by Y'),
# spoken back on resume so the user knows what's about to play.
queue_descriptions = {}

# device_id -> playlist_id of the playlist currently loaded into that
# device's queue, if any (None/absent when the queue holds something else,
# e.g. an album). Lets play_playlist_by_id() tell whether it's safe to
# snapshot the outgoing queue as "playlist X's saved position" before
# clearing it for a new one - see saved_playlist_queues below.
current_playlist_id = {}

# device_id -> {playlist_id: MediaQueue.dump() snapshot}. Populated when a
# device switches away from one playlist to another, so asking to play the
# first one again resumes where it was left off instead of restarting at
# track 1. Only covers playlist-to-playlist switches: switching to an
# album/artist/etc. and back to the same playlist still restarts it, since
# those handlers clear current_playlist_id (via start_new_queue()) rather
# than saving a snapshot.
saved_playlist_queues = {}

logger.debug('MediaQueue manager ready...')


def get_device_id(handler_input: HandlerInput) -> str:
    """Return the requesting Alexa device's unique ID.

    Used to key per-device playback state (queue, background process,
    description) so multiple Echo devices don't share one queue.

    :param HandlerInput handler_input: The Amazon Alexa HandlerInput object
    :return: The device ID
    :rtype: str
    """
    return handler_input.request_envelope.context.system.device.device_id


def get_play_queue(handler_input: HandlerInput):
    """Get (creating if necessary) the MediaQueue for the requesting device.

    :param HandlerInput handler_input: The Amazon Alexa HandlerInput object
    :return: The device's MediaQueue
    """
    device_id = get_device_id(handler_input)

    if device_id not in play_queues:
        play_queues[device_id] = manager.MediaQueue()

    return play_queues[device_id]


def start_new_queue(device_id: str, play_queue) -> None:
    """Clear a device's queue to start a fresh, non-playlist source
    (album, artist, genre, song, random, favourites).

    Also drops any 'currently active playlist' bookkeeping for this device.
    Without this, if a playlist was playing before this new source started,
    a later "play playlist X" could mistake this fresh (non-playlist) queue
    for playlist X still being loaded, and snapshot the wrong tracks under
    X's saved-queue slot in saved_playlist_queues.

    :param str device_id: The requesting Alexa device's unique ID
    :param play_queue: The device's MediaQueue
    :return: None
    """

    play_queue.clear()
    current_playlist_id.pop(device_id, None)

# Connect to Navidrome
connection = api.SubsonicConnection(navidrome_url,
                                    navidrome_user,
                                    navidrome_passwd,
                                    navidrome_port,
                                    navidrome_api_location,
                                    navidrome_api_version)

try:
    connection.ping()

except:
    raise RuntimeError('Could not connect to SubSonic API!')

logger.info('AskNavidrome Web Service is ready to start!')


#
# Handler Classes
#

class LaunchRequestHandler(AbstractRequestHandler):
    """Handle LaunchRequest and NavigateHomeIntent"""

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (
            is_request_type('LaunchRequest')(handler_input) or
            is_intent_name('AMAZON.NavigateHomeIntent')(handler_input)
        )

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In LaunchRequestHandler')

        connection.ping()
        speech = sanitise_speech_output('Ready!')

        handler_input.response_builder.speak(speech).ask(speech)
        return handler_input.response_builder.response


class CheckAudioInterfaceHandler(AbstractRequestHandler):
    """Check if device supports audio play.

    This can be used as the first handler to be checked, before invoking
    other handlers, thus making the skill respond to unsupported devices
    without doing much processing.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        if handler_input.request_envelope.context.system.device:
            # Since skill events won't have device information
            return handler_input.request_envelope.context.system.device.supported_interfaces.audio_player is None
        else:
            return False

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In CheckAudioInterfaceHandler')

        _ = handler_input.attributes_manager.request_attributes['_']
        handler_input.response_builder.speak('This device is not supported').set_should_end_session(True)

        return handler_input.response_builder.response


class SkillEventHandler(AbstractRequestHandler):
    """Close session for skill events or when session ends.

    Handler to handle session end or skill events (SkillEnabled,
    SkillDisabled etc.)
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (handler_input.request_envelope.request.object_type.startswith(
                'AlexaSkillEvent') or
                is_request_type('SessionEndedRequest')(handler_input))

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In SkillEventHandler')

        return handler_input.response_builder.response


HELP_TOPICS = {
    'playing music': (
        'You can say things like: play songs by an artist, play the album X by Y, '
        'play the song X, play some jazz or rock music, play a random selection, '
        'or play my favourite songs.'
    ),
    'finding playlists': (
        "You can say what playlists do I have, and I'll read some out — say hear some more "
        'to keep going. Or say find me a playlist about a mood or genre, like upbeat or blues, '
        "and I'll find one for you. Once you know the name, say play the playlist, followed by the name."
    ),
    'playback controls': (
        'While something is playing, you can say pause, resume, next, previous, shuffle the queue, '
        "what's playing, or star this song to add it to your favourites."
    ),
}


def classify_help_topic(text: str) -> Union[str, None]:
    """Match freeform help-topic text to one of HELP_TOPICS by keyword,
    rather than relying solely on the interaction model's entity
    resolution (which can fail for phrasing not in the slot type's
    synonym list).

    :param str text: The spoken topic, as resolved or raw slot text
    :return: A key into HELP_TOPICS, or None if nothing matched
    :rtype: str | None
    """
    normalized = text.lower()

    if any(word in normalized for word in ('playlist', 'mood', 'genre')):
        return 'finding playlists'
    if any(word in normalized for word in ('control', 'pause', 'skip', 'shuffle', 'resume', 'stop')):
        return 'playback controls'
    if any(word in normalized for word in ('music', 'play', 'song', 'album', 'artist')):
        return 'playing music'

    return None


class HelpHandler(AbstractRequestHandler):
    """Handle HelpIntent"""

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('AMAZON.HelpIntent')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In HelpHandler')

        text = sanitise_speech_output(
            'AskNavidrome lets you play music from your collection and control playback. '
            'I can walk you through playing music, finding playlists, or playback controls. '
            'Which would you like help with?'
        )
        handler_input.response_builder.speak(text).ask(text)

        return handler_input.response_builder.response


class NaviSonicHelpTopic(AbstractRequestHandler):
    """Handle NaviSonicHelpTopic

    Follow-up to AMAZON.HelpIntent - gives focused help on whichever topic
    the user picked (playing music, finding playlists, or playback
    controls) instead of one long info dump.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicHelpTopic')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicHelpTopic')

        topic = get_slot_value_v2(handler_input, 'topic')
        topic_text = resolved_slot_value(topic)

        matched_topic = classify_help_topic(topic_text)

        if matched_topic is None:
            text = sanitise_speech_output(
                "I didn't catch that. You can ask for help with playing music, finding playlists, "
                'or playback controls - which would you like?'
            )
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        prompt = 'Would you like help with something else, or are you ready to try it?'
        text = sanitise_speech_output(f'{HELP_TOPICS[matched_topic]} {prompt}')
        handler_input.response_builder.speak(text).ask(sanitise_speech_output(prompt))

        return handler_input.response_builder.response


class NaviSonicPlayMusicByArtist(AbstractRequestHandler):
    """Handle NaviSonicPlayMusicByArtist

    Play a selection of songs for the given artist
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayMusicByArtist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayMusicByArtist')

        device_id = get_device_id(handler_input)
        play_queue = get_play_queue(handler_input)

        # Check if a background process is already running for this device, if
        # it is then terminate the process in favour of the new process.
        existing_process = background_processes.get(device_id)
        if existing_process is not None:
            existing_process.terminate()
            existing_process.join()

        # Get the requested artist
        artist = get_slot_value_v2(handler_input, 'artist')

        # Search for an artist
        artist_lookup = connection.search_artist(artist.value)

        if artist_lookup is None:
            text = sanitise_speech_output(f"I couldn't find the artist {artist.value} in the collection.")
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        else:
            # Get a list of albums by the artist
            artist_album_lookup = connection.albums_by_artist(artist_lookup[0].get('id'))

            # Build a list of songs to play
            song_id_list = connection.build_song_list_from_albums(artist_album_lookup, min_song_count)
            start_new_queue(device_id, play_queue)

            controller.enqueue_songs(connection, play_queue, song_id_list[:2])  # When generating the playlist return the first two tracks.
            background_processes[device_id] = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))  # Create a thread to enqueue the remaining tracks
            background_processes[device_id].start()  # Start the additional thread

            queue_descriptions[device_id] = f'music by {artist.value}'
            speech = sanitise_speech_output(f'Playing music by: {artist.value}')
            logger.info(speech)

            card = {'title': 'AskNavidrome',
                    'text': speech
                    }

            play_queue.shuffle()
            track_details = play_queue.get_next_track()
            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayAlbumByArtist(AbstractRequestHandler):
    """Handle NaviSonicPlayAlbumByArtist

    Play a given album by a given artist
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayAlbumByArtist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayAlbumByArtist')

        device_id = get_device_id(handler_input)
        play_queue = get_play_queue(handler_input)

        # Check if a background process is already running for this device, if
        # it is then terminate the process in favour of the new process.
        existing_process = background_processes.get(device_id)
        if existing_process is not None:
            existing_process.terminate()
            existing_process.join()

        # Get variables from intent
        artist = get_slot_value_v2(handler_input, 'artist')
        album = get_slot_value_v2(handler_input, 'album')

        if artist is not None and album is not None:
            # Play album by artist method
            logger.debug(f'Searching for the album {album.value} by {artist.value}')

            # Search for an artist
            artist_lookup = connection.search_artist(artist.value)

            if artist_lookup is None:
                text = sanitise_speech_output(f"I couldn't find the artist {artist.value} in the collection.")
                handler_input.response_builder.speak(text).ask(text)

                return handler_input.response_builder.response

            else:
                artist_album_lookup = connection.albums_by_artist(artist_lookup[0].get('id'))

                # Search the list of dictionaries for the requested album
                # Strings are all converted to lower case to minimise matching errors
                result = [album_result for album_result in artist_album_lookup if album_result.get('name').lower() == album.value.lower()]

                if not result:
                    text = sanitise_speech_output(f"I couldn't find an album called {album.value} by {artist.value} in the collection.")
                    handler_input.response_builder.speak(text).ask(text)

                    return handler_input.response_builder.response

                # At this point we have found an album that matches
                song_id_list = connection.build_song_list_from_albums(result, -1)
                start_new_queue(device_id, play_queue)

                # Work around the Amazon / Alexa 8 second timeout.
                controller.enqueue_songs(connection, play_queue, song_id_list[:2])  # When generating the playlist return the first two tracks.
                background_processes[device_id] = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))  # Create a thread to enqueue the remaining tracks
                background_processes[device_id].start()  # Start the additional thread

                queue_descriptions[device_id] = f'the album {album.value} by {artist.value}'
                speech = sanitise_speech_output(f'Playing {album.value} by: {artist.value}')
                logger.info(speech)
                card = {'title': 'AskNavidrome',
                        'text': speech
                        }
                track_details = play_queue.get_next_track()

                return controller.start_playback('play', speech, card, track_details, handler_input)

        elif artist is None and album:
            # Play album method
            logger.debug(f'Searching for the album {album.value}')

            result = connection.search_album(album.value)
            song_id_list = connection.build_song_list_from_albums(result, -1) if result is not None else []

            if not song_id_list:
                # Bare "Play {album}" has no carrier word distinguishing it
                # from a bare song or playlist request, so Alexa's NLU can
                # route either one here. Before giving up, try the same
                # term as a song title, then a playlist name. Also covers
                # Navidrome's catalog search matching some unrelated,
                # near-empty "album" for a term that was never an album at
                # all (e.g. a playlist name) - that's not a usable match either.
                response = find_and_play_bare_term(album.value, handler_input, exclude=frozenset({'album'}))

                if response is not None:
                    return response

                text = sanitise_speech_output(f"I couldn't find the album {album.value} in the collection.")
                handler_input.response_builder.speak(text).ask(text)

                return handler_input.response_builder.response

            else:
                start_new_queue(device_id, play_queue)

                # Work around the Amazon / Alexa 8 second timeout.
                controller.enqueue_songs(connection, play_queue, song_id_list[:2])  # When generating the playlist return the first two tracks.
                background_processes[device_id] = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))  # Create a thread to enqueue the remaining tracks
                background_processes[device_id].start()  # Start the additional thread

                queue_descriptions[device_id] = f'the album {album.value}'
                speech = sanitise_speech_output(f'Playing {album.value}')
                logger.info(speech)
                card = {'title': 'AskNavidrome',
                        'text': speech
                        }
                track_details = play_queue.get_next_track()

                return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlaySongByArtist(AbstractRequestHandler):
    """Handle the NaviSonicPlaySongByArtist intent

    Play the given song by the given artist if it exists in the
    collection.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlaySongByArtist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlaySongByArtist')

        device_id = get_device_id(handler_input)
        play_queue = get_play_queue(handler_input)

        # Get variables from intent
        artist = get_slot_value_v2(handler_input, 'artist')
        song = get_slot_value_v2(handler_input, 'song')

        if artist is None or not artist.value:
            # Bare "Play the song {song}" has no carrier word distinguishing
            # it from a bare album or playlist request, so Alexa's NLU can
            # route either one here. Before giving up, try the term as a
            # song title alone, then an album title, then a playlist name.
            logger.debug(f'Searching for {song.value} with no artist specified')

            response = find_and_play_bare_term(song.value, handler_input)

            if response is not None:
                return response

            text = sanitise_speech_output(f"I couldn't find {song.value} in the collection.")
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        logger.debug(f'Searching for the song {song.value} by {artist.value}')

        # Search for the artist
        artist_lookup = connection.search_artist(artist.value)

        if artist_lookup is None:
            text = sanitise_speech_output(f"I couldn't find the artist {artist.value} in the collection.")
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        else:
            artist_id = artist_lookup[0].get('id')

            # Search for song
            song_list = connection.search_song(song.value)

            # Search for song by given artist.
            song_dets = [item.get('id') for item in song_list or [] if item.get('artistId') == artist_id]

            if not song_dets:
                text = sanitise_speech_output(f"I couldn't find a song called {song.value} by {artist.value} in the collection.")
                handler_input.response_builder.speak(text).ask(text)

                return handler_input.response_builder.response

            start_new_queue(device_id, play_queue)
            controller.enqueue_songs(connection, play_queue, song_dets)

            queue_descriptions[device_id] = f'{song.value} by {artist.value}'
            speech = sanitise_speech_output(f'Playing {song.value} by {artist.value}')
            logger.info(speech)
            card = {'title': 'AskNavidrome',
                    'text': speech
                    }
            track_details = play_queue.get_next_track()

            return controller.start_playback('play', speech, card, track_details, handler_input)


def find_and_play_song(term: str, handler_input: HandlerInput) -> Union[Response, None]:
    """Search for a song matching `term` by title alone (no artist filter)
    and, if found, start playback. See find_and_play_bare_term().

    :param str term: The song title to search for
    :param HandlerInput handler_input: The Amazon Alexa HandlerInput object
    :return: An Alexa Response if a matching song was found and started, else None
    :rtype: Response | None
    """
    device_id = get_device_id(handler_input)
    play_queue = get_play_queue(handler_input)

    song_list = connection.search_song(term)

    if not song_list:
        return None

    song_dets = [item.get('id') for item in song_list]

    start_new_queue(device_id, play_queue)
    controller.enqueue_songs(connection, play_queue, song_dets)

    queue_descriptions[device_id] = str(term)
    speech = sanitise_speech_output(f'Playing {term}')
    logger.info(speech)
    card = {'title': 'AskNavidrome',
            'text': speech
            }
    track_details = play_queue.get_next_track()

    return controller.start_playback('play', speech, card, track_details, handler_input)


def find_and_play_album(term: str, handler_input: HandlerInput) -> Union[Response, None]:
    """Search for an album matching `term` by title alone (no artist filter)
    and, if found, start playback. See find_and_play_bare_term().

    :param str term: The album title to search for
    :param HandlerInput handler_input: The Amazon Alexa HandlerInput object
    :return: An Alexa Response if a matching album was found and started, else None
    :rtype: Response | None
    """
    device_id = get_device_id(handler_input)
    play_queue = get_play_queue(handler_input)

    result = connection.search_album(term)
    song_id_list = connection.build_song_list_from_albums(result, -1) if result is not None else []

    if not song_id_list:
        # A "matched" album with no actual tracks (e.g. bad catalog data)
        # isn't a usable result either.
        return None

    start_new_queue(device_id, play_queue)

    # Work around the Amazon / Alexa 8 second timeout.
    controller.enqueue_songs(connection, play_queue, song_id_list[:2])  # When generating the playlist return the first two tracks.
    background_processes[device_id] = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))  # Create a thread to enqueue the remaining tracks
    background_processes[device_id].start()  # Start the additional thread

    queue_descriptions[device_id] = f'the album {term}'
    speech = sanitise_speech_output(f'Playing {term}')
    logger.info(speech)
    card = {'title': 'AskNavidrome',
            'text': speech
            }
    track_details = play_queue.get_next_track()

    return controller.start_playback('play', speech, card, track_details, handler_input)


def find_and_play_bare_term(term: str, handler_input: HandlerInput, exclude: frozenset = frozenset()) -> Union[Response, None]:
    """Try resolving a bare (no-artist) search term as a song, then an
    album, then a playlist, returning the first successful match.

    Several intents share the same ambiguous "Play {X}" shape with no
    carrier word to tell them apart (song title vs. album title vs.
    playlist name), so Alexa's NLU routing between them is a guess. Rather
    than trusting that guess, whichever intent it lands on tries every
    remaining interpretation here before giving up.

    :param str term: The search term, as captured by whichever intent Alexa matched
    :param HandlerInput handler_input: The Amazon Alexa HandlerInput object
    :param frozenset exclude: Interpretations to skip (already tried by the caller)
    :return: An Alexa Response if any interpretation matched and started, else None
    :rtype: Response | None
    """
    finders = (
        ('song', find_and_play_song),
        ('album', find_and_play_album),
        ('playlist', find_and_play_playlist),
    )

    for kind, finder in finders:
        if kind in exclude:
            continue

        response = finder(term, handler_input)

        if response is not None:
            return response

    return None


def speak_playlist_choices(candidate_names: list, handler_input: HandlerInput) -> Response:
    """Build a clarifying "did you mean X, Y, or Z?" response and keep the
    session open for the user's follow-up.

    :param list candidate_names: 2+ playlist names to offer as choices
    :param HandlerInput handler_input: The Amazon Alexa HandlerInput object
    :return: An Alexa Response asking the user to pick one
    :rtype: Response
    """
    if len(candidate_names) == 2:
        options = f'{candidate_names[0]}, or {candidate_names[1]}'
    else:
        options = ', '.join(candidate_names[:-1]) + f', or {candidate_names[-1]}'

    text = sanitise_speech_output(f'I found a few playlists that might match — did you mean {options}?')
    handler_input.response_builder.speak(text).ask(text)

    return handler_input.response_builder.response


def play_playlist_by_id(playlist_id: str, playlist_name: str, handler_input: HandlerInput) -> Response:
    """Start playback of a known playlist by id.

    :param str playlist_id: The playlist's id
    :param str playlist_name: The playlist's display name, used for speech/description
    :param HandlerInput handler_input: The Amazon Alexa HandlerInput object
    :return: An Alexa Response
    :rtype: Response
    """
    device_id = get_device_id(handler_input)
    play_queue = get_play_queue(handler_input)

    # Snapshot whatever playlist is currently loaded (if it's a different
    # one than requested) before it's replaced or overwritten below, so it
    # can be resumed later too - covers switching between more than two
    # playlists, not just A/B.
    previous_playlist_id = current_playlist_id.get(device_id)
    if previous_playlist_id is not None and previous_playlist_id != playlist_id:
        saved_playlist_queues.setdefault(device_id, {})[previous_playlist_id] = play_queue.dump()

    device_saved_queues = saved_playlist_queues.get(device_id, {})
    snapshot = device_saved_queues.pop(playlist_id, None)

    if snapshot is not None:
        # This device was previously part-way through this same playlist
        # (before switching to a different one) - restore that position
        # instead of rebuilding the queue and starting over at track 1.
        play_queue.restore(snapshot)

        current_playlist_id[device_id] = playlist_id
        queue_descriptions[device_id] = 'the playlist ' + str(playlist_name)
        speech = sanitise_speech_output('Resuming playlist ' + str(playlist_name))
        logger.info(speech)
        card = {'title': 'AskNavidrome',
                'text': speech
                }
        track_details = play_queue.get_current_track()

        return controller.start_playback('play', speech, card, track_details, handler_input)

    song_id_list = connection.build_song_list_from_playlist(playlist_id)

    if not song_id_list:
        text = sanitise_speech_output(f'The playlist {playlist_name} is empty. There is nothing to play.')
        handler_input.response_builder.speak(text).ask(text)

        return handler_input.response_builder.response

    play_queue.clear()

    # Work around the Amazon / Alexa 8 second timeout.
    controller.enqueue_songs(connection, play_queue, song_id_list[:2])  # When generating the playlist return the first two tracks.
    background_processes[device_id] = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))  # Create a thread to enqueue the remaining tracks
    background_processes[device_id].start()  # Start the additional thread

    current_playlist_id[device_id] = playlist_id
    queue_descriptions[device_id] = 'the playlist ' + str(playlist_name)
    speech = sanitise_speech_output('Playing playlist ' + str(playlist_name))
    logger.info(speech)
    card = {'title': 'AskNavidrome',
            'text': speech
            }
    track_details = play_queue.get_next_track()

    return controller.start_playback('play', speech, card, track_details, handler_input)


def find_and_play_playlist(term: str, handler_input: HandlerInput) -> Union[Response, None]:
    """Search for a playlist matching `term` and, if found, start playback.

    Factored out of NaviSonicPlayPlaylist so other intents whose bare
    "Play {X}" phrasing is structurally indistinguishable from a playlist
    request (e.g. NaviSonicPlayAlbumByArtist's bare "Play {album}", with no
    carrier word to tell Alexa's NLU which intent was meant) can fall back
    to trying the same term as a playlist name before giving up. Assumes
    the caller has already handled any in-flight background process for
    this device.

    With dozens of dynamically-named AudioMuse playlists in play, a "best
    guess" match is no longer safe to play silently when several playlists
    score similarly close to the spoken term — in that case this asks the
    user to disambiguate instead of guessing wrong.

    :param str term: The playlist name to search for
    :param HandlerInput handler_input: The Amazon Alexa HandlerInput object
    :return: An Alexa Response (playing a match, or asking to disambiguate) if anything close was found, else None
    :rtype: Response | None
    """
    ranked = connection.rank_playlists(term)

    if not ranked or ranked[0][0] < 0.6:
        return None

    top_score, top_id, top_name = ranked[0]

    if top_score < 0.97 and len(ranked) > 1 and (top_score - ranked[1][0]) < 0.15:
        close_candidates = [name for score, _, name in ranked[1:3] if (top_score - score) < 0.15]

        return speak_playlist_choices([top_name] + close_candidates, handler_input)

    return play_playlist_by_id(top_id, top_name, handler_input)


class NaviSonicPlayPlaylist(AbstractRequestHandler):
    """Handle NaviSonicPlayPlaylist

    Play the given playlist
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayPlaylist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayPlaylist')

        device_id = get_device_id(handler_input)

        # Check if a background process is already running for this device, if
        # it is then terminate the process in favour of the new process.
        existing_process = background_processes.get(device_id)
        if existing_process is not None:
            existing_process.terminate()
            existing_process.join()

        # Get the requested playlist
        playlist = get_slot_value_v2(handler_input, 'playlist')
        playlist_name = resolved_slot_value(playlist)

        response = find_and_play_playlist(playlist_name, handler_input)

        if response is not None:
            return response

        text = sanitise_speech_output("I couldn't find the playlist " + str(playlist_name) + ' in the collection.')
        handler_input.response_builder.speak(text).ask(text)

        return handler_input.response_builder.response


def speak_playlist_page(names: list, offset: int, handler_input: HandlerInput, page_size: int = 8) -> Response:
    """Speak playlist names starting at `offset`, prompting the user to
    either play one or hear more, and remember position in session
    attributes so a follow-up "hear more" can continue from here.

    :param list names: All playlist names, in the order to page through
    :param int offset: Index of the first name to speak this turn
    :param HandlerInput handler_input: The Amazon Alexa HandlerInput object
    :param int page_size: How many names to speak per turn
    :return: An Alexa Response
    :rtype: Response
    """
    page = names[offset:offset + page_size]
    remaining = len(names) - (offset + len(page))

    intro = f'You have {len(names)} playlists. Here are some: ' if offset == 0 else 'Here are some more: '

    if remaining > 0:
        prompt = 'Do you want to play one of these, or hear some more?'

        handler_input.attributes_manager.session_attributes = {
            'playlist_names': names,
            'playlist_offset': offset + len(page),
        }
    else:
        prompt = "That's all of them. Do you want to play one of these?"

        # Reached the end: nothing left to page through this session.
        handler_input.attributes_manager.session_attributes = {}

    speech = sanitise_speech_output(intro + ', '.join(page) + '. ' + prompt)
    logger.info(speech)

    handler_input.response_builder.set_card(
        StandardCard(title='AskNavidrome Playlists', text='\n'.join(names))
    )
    handler_input.response_builder.speak(speech).ask(sanitise_speech_output(prompt))

    return handler_input.response_builder.response


class NaviSonicListPlaylists(AbstractRequestHandler):
    """Handle NaviSonicListPlaylists

    Read back the names of available playlists, to help find the right one
    among dozens of dynamically-named AudioMuse playlists.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicListPlaylists')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicListPlaylists')

        names = sorted(item.get('name') for item in connection.get_all_playlists())

        if not names:
            text = sanitise_speech_output("You don't have any playlists yet.")
            handler_input.response_builder.speak(text)

            return handler_input.response_builder.response

        return speak_playlist_page(names, 0, handler_input)


class NaviSonicHearMorePlaylists(AbstractRequestHandler):
    """Handle NaviSonicHearMorePlaylists

    Continue reading the playlist list from where NaviSonicListPlaylists
    (or a previous "hear more") left off, using session attributes to
    track position.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicHearMorePlaylists')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicHearMorePlaylists')

        session_attrs = handler_input.attributes_manager.session_attributes
        names = session_attrs.get('playlist_names')
        offset = session_attrs.get('playlist_offset', 0)

        if not names:
            # No list in progress this session (e.g. "hear more" out of the
            # blue) - start one fresh rather than erroring.
            names = sorted(item.get('name') for item in connection.get_all_playlists())
            offset = 0

        return speak_playlist_page(names, offset, handler_input)


class NaviSonicFindPlaylist(AbstractRequestHandler):
    """Handle NaviSonicFindPlaylist

    Search playlist names and descriptions for a mood/genre/keyword match
    (e.g. "find me an upbeat playlist") and play the best match, or ask
    which one was meant when several are close.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicFindPlaylist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicFindPlaylist')

        device_id = get_device_id(handler_input)

        # Check if a background process is already running for this device, if
        # it is then terminate the process in favour of the new process.
        existing_process = background_processes.get(device_id)
        if existing_process is not None:
            existing_process.terminate()
            existing_process.join()

        query = get_slot_value_v2(handler_input, 'query')

        results = connection.search_playlists_by_keyword(query.value)

        if not results:
            text = sanitise_speech_output(f"I couldn't find a playlist matching {query.value}.")
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        top_score, top_id, top_name = results[0]

        # A clear standout (every query word matched, and well ahead of the
        # next candidate) is confident enough to just play; otherwise ask.
        if top_score >= 1.0 and (len(results) == 1 or results[1][0] < top_score - 0.3):
            return play_playlist_by_id(top_id, top_name, handler_input)

        candidate_names = [name for _, _, name in results[:3]]

        return speak_playlist_choices(candidate_names, handler_input)


class NaviSonicPlayMusicByGenre(AbstractRequestHandler):
    """ Play songs from the given genre

    50 tracks from the given genre are shuffled and played
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayMusicByGenre')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayMusicByGenre')

        device_id = get_device_id(handler_input)
        play_queue = get_play_queue(handler_input)

        # Check if a background process is already running for this device, if
        # it is then terminate the process in favour of the new process.
        existing_process = background_processes.get(device_id)
        if existing_process is not None:
            existing_process.terminate()
            existing_process.join()

        # Get the requested genre
        genre = get_slot_value_v2(handler_input, 'genre')

        song_id_list = connection.build_song_list_from_genre(genre.value, min_song_count)

        if song_id_list is None:
            text = sanitise_speech_output(f"I couldn't find any {genre.value} songs in the collection.")
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        else:
            random.shuffle(song_id_list)
            start_new_queue(device_id, play_queue)

            # Work around the Amazon / Alexa 8 second timeout.
            controller.enqueue_songs(connection, play_queue, song_id_list[:2])  # When generating the playlist return the first two tracks.
            background_processes[device_id] = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))  # Create a thread to enqueue the remaining tracks
            background_processes[device_id].start()  # Start the additional thread

            queue_descriptions[device_id] = f'{genre.value} music'
            speech = sanitise_speech_output(f'Playing {genre.value} music')
            logger.info(speech)
            card = {'title': 'AskNavidrome',
                    'text': speech
                    }
            track_details = play_queue.get_next_track()

            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayMusicRandom(AbstractRequestHandler):
    """Handle the NaviSonicPlayMusicRandom intent

    Play a random selection of music.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayMusicRandom')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayMusicRandom')

        device_id = get_device_id(handler_input)
        play_queue = get_play_queue(handler_input)

        # Check if a background process is already running for this device, if
        # it is then terminate the process in favour of the new process.
        existing_process = background_processes.get(device_id)
        if existing_process is not None:
            existing_process.terminate()
            existing_process.join()

        song_id_list = connection.build_random_song_list(min_song_count)

        if song_id_list is None:
            text = sanitise_speech_output("I couldn't find any songs in the collection.")
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        else:
            random.shuffle(song_id_list)
            start_new_queue(device_id, play_queue)

            # Work around the Amazon / Alexa 8 second timeout.
            controller.enqueue_songs(connection, play_queue, song_id_list[:2])  # When generating the playlist return the first two tracks.
            background_processes[device_id] = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))  # Create a thread to enqueue the remaining tracks
            background_processes[device_id].start()  # Start the additional thread

            queue_descriptions[device_id] = 'random music'
            speech = sanitise_speech_output('Playing random music')
            logger.info(speech)
            card = {'title': 'AskNavidrome',
                    'text': speech
                    }
            track_details = play_queue.get_next_track()

            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayFavouriteSongs(AbstractRequestHandler):
    """Handle the NaviSonicPlayFavouriteSongs intent

    Play all starred / liked songs, songs are automatically shuffled.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayFavouriteSongs')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayFavouriteSongs')

        device_id = get_device_id(handler_input)
        play_queue = get_play_queue(handler_input)

        # Check if a background process is already running for this device, if
        # it is then terminate the process in favour of the new process.
        existing_process = background_processes.get(device_id)
        if existing_process is not None:
            existing_process.terminate()
            existing_process.join()

        song_id_list = connection.build_song_list_from_favourites()

        if song_id_list is None:
            text = sanitise_speech_output("You don't have any favourite songs in the collection.")
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        else:
            random.shuffle(song_id_list)
            start_new_queue(device_id, play_queue)

            # Work around the Amazon / Alexa 8 second timeout.
            controller.enqueue_songs(connection, play_queue, song_id_list[:2])  # When generating the playlist return the first two tracks.
            background_processes[device_id] = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))  # Create a thread to enqueue the remaining tracks
            background_processes[device_id].start()  # Start the additional thread

            queue_descriptions[device_id] = 'your favourite tracks'
            speech = sanitise_speech_output('Playing your favourite tracks.')
            logger.info(speech)
            card = {'title': 'AskNavidrome',
                    'text': speech
                    }
            track_details = play_queue.get_next_track()

            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicRandomiseQueue(AbstractRequestHandler):
    """Handle NaviSonicRandomiseQueue Intent

    Shuffle the current play queue
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicRandomiseQueue')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicRandomiseQueue Handler')

        play_queue = get_play_queue(handler_input)
        play_queue.shuffle()
        play_queue.sync()

        return handler_input.response_builder.response


class NaviSonicSongDetails(AbstractRequestHandler):
    """Handle NaviSonicSongDetails Intent

    Returns information on the track that is currently playing
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicSongDetails')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicSongDetails Handler')

        play_queue = get_play_queue(handler_input)
        current_track = play_queue.get_current_track()

        title = sanitise_speech_output(current_track.title)
        artist = sanitise_speech_output(current_track.artist)
        album = sanitise_speech_output(current_track.album)

        text = f'This is {title} by {artist}, from the album {album}'
        handler_input.response_builder.speak(text)

        return handler_input.response_builder.response


class NaviSonicStarSong(AbstractRequestHandler):
    """Handle NaviSonicStarSong Intent

    Star / favourite the current song
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicStarSong')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicStarSong Handler')

        play_queue = get_play_queue(handler_input)
        current_track = play_queue.get_current_track()

        song_id = current_track.id
        connection.star_entry(song_id, 'song')

        return handler_input.response_builder.response


class NaviSonicUnstarSong(AbstractRequestHandler):
    """Handle NaviSonicUnstarSong Intent

    Star / favourite the current song
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicUnstarSong')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicUnstarSong Handler')

        play_queue = get_play_queue(handler_input)
        current_track = play_queue.get_current_track()

        song_id = current_track.id
        connection.star_entry(song_id, 'song')
        connection.unstar_entry(song_id, 'song')

        return handler_input.response_builder.response

#
# AudioPlayer Handlers
#


class PlaybackStartedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackStarted Directive received.

    Confirming that the requested audio file began playing.
    Do not send any specific response.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackStarted')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackStartedHandler')
        logger.info('Playback started')

        # Report the track to Navidrome's now-playing list. The token is the
        # Navidrome track ID (see controller.start_playback). Never let this
        # break playback.
        try:
            connection.now_playing(handler_input.request_envelope.request.token,
                                   get_device_id(handler_input))
        except Exception as e:
            logger.warning(f'Failed to report now playing to Navidrome: {e}')

        return handler_input.response_builder.response


class PlaybackStoppedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackStopped Directive received.

    Confirming that the requested audio file stopped playing.
    Do not send any specific response.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackStopped')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackStoppedHandler')

        play_queue = get_play_queue(handler_input)

        # store the current offset for later resumption
        play_queue.set_current_track_offset(handler_input.request_envelope.request.offset_in_milliseconds)

        current_track = play_queue.get_current_track()
        logger.debug(f'Stored track offset of: {current_track.offset} ms for {current_track.title}')
        logger.info('Playback stopped')

        return handler_input.response_builder.response


class PlaybackNearlyFinishedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackNearlyFinished Directive received.

    Replacing queue with the URL again. This should not happen on live streams.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackNearlyFinished')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackNearlyFinishedHandler')
        logger.info('Queuing next track...')
        play_queue = get_play_queue(handler_input)
        track_details = play_queue.enqueue_next_track()

        if track_details is None:
            # Nothing left in the buffer - the current track is the last
            # one queued, so there's nothing more to enqueue right now.
            logger.debug('Buffer empty, nothing to enqueue')
            return handler_input.response_builder.response

        return controller.start_playback('continue', None, None, track_details, handler_input)


class PlaybackFinishedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackFinished Directive received.

    Confirming that the requested audio file completed playing.
    Do not send any specific response.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackFinished')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackFinishedHandler')

        play_queue = get_play_queue(handler_input)

        # Generate a timestamp in milliseconds for scrobbling
        timestamp_ms = datetime.now().timestamp()
        current_track = play_queue.get_current_track()
        connection.scrobble(current_track.id, timestamp_ms)
        play_queue.get_next_track()

        return handler_input.response_builder.response


class PausePlaybackHandler(AbstractRequestHandler):
    """Handler for stopping audio.

    Handles Stop, Cancel and Pause Intents and PauseCommandIssued event.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (is_intent_name('AMAZON.StopIntent')(handler_input) or
                is_intent_name('AMAZON.CancelIntent')(handler_input) or
                is_intent_name('AMAZON.PauseIntent')(handler_input))

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PausePlaybackHandler')
        play_queue = get_play_queue(handler_input)
        play_queue.sync()

        return controller.stop(handler_input)


class ResumePlaybackHandler(AbstractRequestHandler):
    """Handler for resuming audio on different events.

    Handles PlayAudio Intent, Resume Intent.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (is_intent_name('AMAZON.ResumeIntent')(handler_input) or
                is_intent_name('PlayAudio')(handler_input))

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In ResumePlaybackHandler')

        device_id = get_device_id(handler_input)
        play_queue = get_play_queue(handler_input)
        current_track = play_queue.get_current_track()
        description = queue_descriptions.get(device_id)

        if description:
            text = sanitise_speech_output(f'Now playing {description}')
        else:
            text = None

        if current_track.offset > 0:
            # There is a paused track, continue
            logger.info('Resuming ' + str(current_track.title))
            logger.info('Offset ' + str(current_track.offset))

            return controller.start_playback('play', text, None, current_track, handler_input)

        elif play_queue.get_queue_count() > 0 and current_track.offset == 0:
            # No paused tracks but tracks in queue
            logger.info('Resuming - There was no paused track, getting next track from queue')
            track_details = play_queue.get_next_track()

            return controller.start_playback('play', text, None, track_details, handler_input)

        else:
            # Nothing paused and nothing queued - there's nothing to resume
            logger.info('Nothing to resume - queue is empty')
            text = sanitise_speech_output("There's nothing queued to play. Try asking for an album, playlist, or artist.")
            handler_input.response_builder.speak(text)

            return handler_input.response_builder.response


class NextPlaybackHandler(AbstractRequestHandler):
    """Handle NextIntent"""

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (is_intent_name('AMAZON.NextIntent')(handler_input) or
                is_request_type('PlaybackController.NextCommandIssued')(handler_input))

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NextPlaybackHandler')

        play_queue = get_play_queue(handler_input)
        track_details = play_queue.get_next_track()

        # Set the offset to 0 as we are skipping we want to start at the beginning
        track_details.offset = 0

        return controller.start_playback('play', None, None, track_details, handler_input)


class PreviousPlaybackHandler(AbstractRequestHandler):
    """Handle PreviousIntent"""

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (is_intent_name('AMAZON.PreviousIntent')(handler_input) or
                is_request_type('PlaybackController.PreviousCommandIssued')(handler_input))

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PreviousPlaybackHandler')
        play_queue = get_play_queue(handler_input)
        track_details = play_queue.get_previous_track()

        # Set the offset to 0 as we are skipping we want to start at the beginning
        track_details.offset = 0

        return controller.start_playback('play', None, None, track_details, handler_input)


class PlaybackFailedEventHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackFailed Directive received.

    Logging the error and restarting playing with no output speech.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackFailed')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackFailedHandler')

        play_queue = get_play_queue(handler_input)
        current_track = play_queue.get_current_track()
        song_id = current_track.id

        # Log failure and track ID
        logger.error(f'Playback Failed: {handler_input.request_envelope.request.error}')
        logger.error(f'Failed playing track with ID: {song_id}')

        # Skip to the next track instead of stopping
        track_details = play_queue.get_next_track()

        # Set the offset to 0 as we are skipping we want to start at the beginning
        track_details.offset = 0

        return controller.start_playback('play', None, None, track_details, handler_input)


#
# Exception Handers
#


class SystemExceptionHandler(AbstractExceptionHandler):
    """Handle System.ExceptionEncountered

    Handles exceptions and prints error information
    in the log
    """

    def can_handle(self, handler_input: HandlerInput, exception: Exception) -> bool:
        return is_request_type('System.ExceptionEncountered')(handler_input)

    def handle(self, handler_input: HandlerInput, exception: Exception) -> Response:
        logger.debug('In SystemExceptionHandler')

        # Log the exception
        logger.error(f'System Exception: {exception}')
        logger.error(f'Request Type Was: {get_request_type(handler_input)}')
        error = handler_input.request_envelope.request.to_dict()
        logger.error(f"Details: {error.get('error').get('message')}")

        if get_request_type(handler_input) == 'IntentRequest':
            logger.error(f'Intent Name Was: {get_intent_name(handler_input)}')

        speech = sanitise_speech_output("Sorry, I didn't get that. Can you please say it again!!")
        handler_input.response_builder.speak(speech).ask(speech)

        return handler_input.response_builder.response


class GeneralExceptionHandler(AbstractExceptionHandler):
    """Handle general exceptions

    Handles exceptions and prints error information
    in the log
    """

    def can_handle(self, handler_input: HandlerInput, exception: Exception) -> bool:
        return True

    def handle(self, handler_input: HandlerInput, exception: Exception) -> Response:
        logger.debug('In GeneralExceptionHandler')

        # Log the exception
        logger.error(f'General Exception: {exception}')
        logger.error(f'Request Type Was: {get_request_type(handler_input)}')

        if get_request_type(handler_input) == 'IntentRequest':
            logger.error(f'Intent Name Was: {get_intent_name(handler_input)}')

        speech = sanitise_speech_output("Sorry, I didn't get that. Can you please say it again!!")
        handler_input.response_builder.speak(speech).ask(speech)

        return handler_input.response_builder.response


#
# Request Interceptors
#


class LoggingRequestInterceptor(AbstractRequestInterceptor):
    """Intercept all requests

    Intercepts all requests sent to the skill and prints them in the log
    """

    def process(self, handler_input: HandlerInput):
        logger.debug(f'Request received: {handler_input.request_envelope.request}')


class LoggingResponseInterceptor(AbstractResponseInterceptor):
    """Intercept all responses

    Intercepts all responses sent from the skill and prints them in the log
    """

    def process(self, handler_input: HandlerInput, response: Response):
        logger.debug(f'Response sent: {response}')

#
# Functions
#


def sanitise_speech_output(speech_string: str) -> str:
    """Sanitise speech output inline with the SSML standard

    Speech Synthesis Markup Language (SSML) has certain ASCII characters that are
    reserved.  This function replaces them with alternatives.

    :param speech_string: The string to process
    :type speech_string: str
    :return: The processed SSML compliant string
    :rtype: str
    """

    logger.debug('In sanitise_speech_output()')

    if '&' in speech_string:
        speech_string = speech_string.replace('&', 'and')
    if '/' in speech_string:
        speech_string = speech_string.replace('/', 'and')
    if '\\' in speech_string:
        speech_string = speech_string.replace('\\', 'and')
    if '"' in speech_string:
        speech_string = speech_string.replace('"', '')
    if "'" in speech_string:
        speech_string = speech_string.replace("'", "")
    if "<" in speech_string:
        speech_string = speech_string.replace('<', '')
    if ">" in speech_string:
        speech_string = speech_string.replace('>', '')

    return speech_string


def queue_worker_thread(connection: object, play_queue: object, song_id_list: list) -> None:
    """Media queue worker

    This function allows media queues to be populated in the background enabling multithreading
    and increasing skill response times.

    :param connection: A SubSonic API connection object
    :type connection: object
    :param play_queue: A MediaQueue object
    :type play_queue: object
    :param song_id_list: A list containing Navidrome song IDs
    :type song_id_list: list
    """

    logger.debug('In playlist processing thread!')
    controller.enqueue_songs(connection, play_queue, song_id_list)
    play_queue.sync()
    logger.debug('Finished playlist processing!')


# Register Intent Handlers
sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(CheckAudioInterfaceHandler())
sb.add_request_handler(SkillEventHandler())
sb.add_request_handler(HelpHandler())
sb.add_request_handler(NaviSonicHelpTopic())
sb.add_request_handler(NaviSonicPlayMusicByArtist())
sb.add_request_handler(NaviSonicPlayAlbumByArtist())
sb.add_request_handler(NaviSonicPlaySongByArtist())
sb.add_request_handler(NaviSonicPlayPlaylist())
sb.add_request_handler(NaviSonicListPlaylists())
sb.add_request_handler(NaviSonicHearMorePlaylists())
sb.add_request_handler(NaviSonicFindPlaylist())
sb.add_request_handler(NaviSonicPlayFavouriteSongs())
sb.add_request_handler(NaviSonicPlayMusicByGenre())
sb.add_request_handler(NaviSonicPlayMusicRandom())
sb.add_request_handler(NaviSonicRandomiseQueue())
sb.add_request_handler(NaviSonicSongDetails())
sb.add_request_handler(NaviSonicStarSong())
sb.add_request_handler(NaviSonicUnstarSong())

# Register AutoPlayer Handlers
sb.add_request_handler(PlaybackStartedHandler())
sb.add_request_handler(PlaybackStoppedHandler())
sb.add_request_handler(PlaybackNearlyFinishedHandler())
sb.add_request_handler(PlaybackFinishedHandler())
sb.add_request_handler(PausePlaybackHandler())
sb.add_request_handler(NextPlaybackHandler())
sb.add_request_handler(PreviousPlaybackHandler())
sb.add_request_handler(ResumePlaybackHandler())
sb.add_request_handler(PlaybackFailedEventHandler())


# Register Exception Handlers
sb.add_exception_handler(SystemExceptionHandler())
sb.add_exception_handler(GeneralExceptionHandler())

if navidrome_log_level >= 2:
    # Register Interceptors (log all requests)
    sb.add_global_request_interceptor(LoggingRequestInterceptor())
    sb.add_global_response_interceptor(LoggingResponseInterceptor())

sa = SkillAdapter(skill=sb.create(), skill_id='test', app=app)
sa.register(app=app, route='/')


@app.errorhandler(MethodNotAllowed)
def hide_endpoint_on_bad_method(_error):
    """Return 404 instead of 405 for non-POST requests.

    The skill endpoint only accepts POST from Alexa. A 405 confirms to
    anyone probing the URL (e.g. a browser GET) that something is
    listening here; a 404 makes it look like nothing exists.
    """
    return NotFound()


@app.errorhandler(BadRequest)
def hide_endpoint_on_failed_verification(_error):
    """Return 404 instead of 400 when Alexa request verification fails.

    SkillAdapter raises BadRequest when the request signature/timestamp
    can't be verified, i.e. it didn't genuinely come from Alexa. Same
    reasoning as above: don't confirm a live endpoint to unverified
    requests.
    """
    return NotFound()

def track_summary(track) -> dict:
    """Minimal JSON-friendly view of a Track for the /status endpoint."""

    return {'id': track.id, 'title': track.title, 'artist': track.artist,
            'album': track.album, 'duration': track.duration}


@app.route('/status')
def playback_status():
    """Per-device playback status for Home Assistant.

    Returns what each device is playing from (queue description, e.g. 'the
    playlist Crossover'), its current track and the next few queued tracks.
    Only served when NAVI_STATUS_TOKEN is set and the request carries it as a
    Bearer token; otherwise 404, same as the skill endpoint's hiding of itself
    from unverified requests (this app is publicly routed via Traefik).
    """

    expected = os.getenv('NAVI_STATUS_TOKEN', '')
    supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')

    if not expected or not hmac.compare_digest(supplied, expected):
        return NotFound()

    try:
        upcoming_count = min(int(request.args.get('upcoming', 5)), 25)
    except ValueError:
        upcoming_count = 5

    devices = []
    for device_id, device_queue in list(play_queues.items()):
        current = device_queue.get_current_track()
        if not current.id:
            continue

        upcoming = list(device_queue.get_current_queue())[:upcoming_count]
        devices.append({
            'device_id': device_id,
            'source': queue_descriptions.get(device_id),
            'playlist_id': current_playlist_id.get(device_id),
            'current': track_summary(current),
            'upcoming': [track_summary(t) for t in upcoming],
            'queue_length': device_queue.get_queue_count(),
        })

    return {'devices': devices}


# Enable queue and history diagnostics
if navidrome_log_level == 3:
    logger.warning('AskNavidrome debugging has been enabled, this should only be used when testing!')
    logger.warning('The /buffer, /queue and /history http endpoints are available publicly!')

    def resolve_debug_queue():
        """Resolve which device's MediaQueue a debug request is for.

        Playback state is now per-device, so these debug routes need to know
        which device to show. Pass ?device=<id> to pick one; otherwise falls
        back to the only active device, or the first if several are active.

        :return: (device_id, MediaQueue) if a device is available, else (None, None)
        :rtype: tuple
        """
        requested_device = request.args.get('device')

        if requested_device:
            return requested_device, play_queues.get(requested_device)

        if not play_queues:
            return None, None

        device_id = next(iter(play_queues))
        return device_id, play_queues[device_id]

    @app.route('/queue')
    def view_queue():
        """View the contents of a device's play_queue.queue

        Creates a tabulated page containing the contents of the play_queue.queue deque.
        """

        device_id, device_queue = resolve_debug_queue()

        if device_queue is None:
            return f'No active playback queue for device {device_id!r}. Active devices: {list(play_queues)}'

        current_track = device_queue.get_current_track()

        return render_template('table.html', title=f'AskNavidrome - Queued Tracks ({device_id})',
                               tracks=device_queue.get_current_queue(), current=current_track)

    @app.route('/history')
    def view_history():
        """View the contents of a device's play_queue.history

        Creates a tabulated page containing the contents of the play_queue.history deque.
        """

        device_id, device_queue = resolve_debug_queue()

        if device_queue is None:
            return f'No active playback queue for device {device_id!r}. Active devices: {list(play_queues)}'

        current_track = device_queue.get_current_track()

        return render_template('table.html', title=f'AskNavidrome - Track History ({device_id})',
                               tracks=device_queue.get_history(), current=current_track)

    @app.route('/buffer')
    def view_buffer():
        """View the contents of a device's play_queue.buffer

        Creates a tabulated page containing the contents of the play_queue.buffer deque.
        """

        device_id, device_queue = resolve_debug_queue()

        if device_queue is None:
            return f'No active playback queue for device {device_id!r}. Active devices: {list(play_queues)}'

        current_track = device_queue.get_current_track()

        return render_template('table.html', title=f'AskNavidrome - Buffered Tracks ({device_id})',
                               tracks=device_queue.get_buffer(), current=current_track)


# Run web app by default when file is executed.
if __name__ == '__main__':
    # Start the web service
    app.run(host='0.0.0.0')
