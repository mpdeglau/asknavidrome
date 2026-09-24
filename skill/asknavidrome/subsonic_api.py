from hashlib import md5
from typing import Union
from urllib.parse import quote
import difflib
import logging
import random
import re
import secrets

import libsonic


class SubsonicConnection:
    """Class with methods to interact with Subsonic API compatible media servers
    """

    # Words to ignore when scoring a keyword/mood search query, so carrier
    # phrasing ("find me a playlist about X") and common connectives don't
    # get treated as meaningful search terms.
    _KEYWORD_STOPWORDS = frozenset({
        'a', 'an', 'the', 'and', 'or', 'of', 'to', 'for', 'with', 'about',
        'me', 'my', 'some', 'something', 'please', 'find', 'suggest',
        'recommend', 'playlist', 'playlists', 'that', 'is',
    })

    def __init__(self, server_url: str, user: str, passwd: str, port: int, api_location: str, api_version: str) -> None:
        """
        :param str server_url: The URL of the Subsonic API compatible media server
        :param str user: Username to authenticate against the API
        :param str passwd: Password to authenticate against the API
        :param int port: Port the Subsonic compatible server is listening on
        :param str api_location: Path to the API, this is appended to server_url
        :param str api_version: The version of the Subsonic API that is in use
        :return: None
        """

        self.logger = logging.getLogger(__name__)

        self.server_url = server_url
        self.user = user
        self.passwd = passwd
        self.port = port
        self.api_location = api_location
        self.api_version = api_version

        self.conn = libsonic.Connection(self.server_url,
                                        self.user,
                                        self.passwd,
                                        self.port,
                                        self.api_location,
                                        'AskNavidrome',
                                        self.api_version,
                                        False)

        self.logger.debug('Connecting to Navidrome.....')

    def ping(self) -> bool:
        """Ping a Subsonic API server

        Verify the connection to a Subsonic compatible API server
        is working.  Enhanced logging has been added to examine the
        HTTP response returned from the server to assist with troubleshooting
        connection issues.

        :return: True if the connection works, False if it does not
        :rtype: bool
        """

        self.logger.debug('In function ping()')
        http_request = self.conn._getRequest('ping.view')
        try:
            http_request_result = self.conn._doInfoReq(http_request)
        except Exception as e:
            self.logger.error('Failed to connect to Navidrome: %s', e, exc_info=True)
            return False

        self.logger.debug('ping() response from server: %s', http_request_result)
        status = http_request_result.get('status')
        if status == 'ok':
            self.logger.info('Successfully connected to Navidrome')
            return True
        elif status == 'failed':
            err = http_request_result.get('error', {})
            self.logger.error('Failed to connect to Navidrome: code=%s msg=%s',
                              err.get('code'), err.get('message'))
            return False
        else:
            self.logger.error('Unexpected error when connecting to Navidrome: %r', status)
            return False

    def scrobble(self, track_id: str, time: int) -> None:
        """Scrobble the given track

        :param str track_id: The ID of the track to scrobble
        :param int time: UNIX timestamp of track play time
        :return: None
        """
        self.logger.debug('In function scrobble()')

        self.conn.scrobble(track_id, True, time)

        return None

    def now_playing(self, track_id: str, device_id: str = '') -> None:
        """Tell Navidrome the given track is now playing

        Sends a non-submission scrobble, which is what populates Navidrome's
        getNowPlaying list (no play count is recorded).

        Navidrome keeps one now-playing entry per user + client name, so with
        every Echo using the same account and client, a second Echo would
        overwrite the first. When device_id is given the scrobble is sent with
        a per-device client name ("AskNavidrome-<last 8 chars of device ID>")
        so each Echo gets its own entry. Only this call uses it; streaming
        still identifies as the normal client name.

        :param str track_id: The ID of the track that started playing
        :param str device_id: The Alexa device ID the track is playing on
        :return: None
        """
        self.logger.debug('In function now_playing()')

        query = {'id': track_id, 'submission': False}
        if device_id:
            query['c'] = f'AskNavidrome-{device_id[-8:]}'

        # libsonic's scrobble() has no way to override the client name, but
        # its request builder lets per-call query params override the base ones.
        req = self.conn._getRequest('scrobble.view', self.conn._getQueryDict(query))
        res = self.conn._doInfoReq(req)
        self.conn._checkStatus(res)

        return None

    @staticmethod
    def _normalize_playlist_name(name: str) -> str:
        """Lowercase and strip everything but letters/digits, so voice-friendly
        slot values (e.g. "crossover") can be compared against stylized
        playlist titles (e.g. "FM-X (The Cross-Over)") without punctuation,
        spacing or casing getting in the way.
        """

        return re.sub(r'[^a-z0-9]', '', name.lower())

    @staticmethod
    def _normalize_search_term(term: str) -> str:
        """As _normalize_playlist_name, but first strips generic carrier
        words (e.g. "playlist"/"playlists") that AMAZON.SearchQuery can
        sweep into the slot value from phrasing like "my {playlist}
        playlist" but that never appear in an actual playlist name, so they
        don't get treated as part of the search term.
        """

        return SubsonicConnection._normalize_playlist_name(re.sub(r'\bplaylists?\b', '', term, flags=re.IGNORECASE))

    @staticmethod
    def _normalize_words(text: str) -> list:
        """Lowercase and split into words, stripping punctuation but
        keeping word boundaries (unlike _normalize_playlist_name, which
        collapses everything into one contiguous blob). Used for keyword/
        mood matching against multi-word text like playlist comments.
        """

        return re.sub(r'[^a-z0-9\s]', ' ', text.lower()).split()

    def get_all_playlists(self) -> list:
        """Return every playlist known to the media server.

        :return: A list of playlist dictionaries (id, name, comment, ...) as returned by getPlaylists()
        :rtype: list
        """

        self.logger.debug('In function get_all_playlists()')

        return self.conn.getPlaylists()['playlists']['playlist']

    def rank_playlists(self, term: str) -> list:
        """Score every playlist's name against `term`, best match first.

        Used instead of a plain first-match search so callers can tell an
        unambiguous match (score close to 1.0, clear of the runner-up) from
        an ambiguous one (several playlists scoring close together) and
        decide whether to just play it or ask which one was meant.

        :param str term: The playlist name as spoken/transcribed
        :return: A list of (score, id, name) tuples sorted by score descending
        :rtype: list
        """

        self.logger.debug('In function rank_playlists()')

        normalized_term = self._normalize_search_term(term)

        scored = []

        for item in self.get_all_playlists():
            name = item.get('name')
            normalized_name = self._normalize_playlist_name(name)

            if normalized_name == normalized_term:
                score = 1.0
            elif normalized_term and normalized_term in normalized_name:
                # A stylized name containing the whole spoken term (e.g.
                # "crossover" inside "FM-X (The Cross-Over)") is a strong
                # signal, but not quite as certain as an exact match.
                score = 0.9
            else:
                score = difflib.SequenceMatcher(None, normalized_term, normalized_name).ratio()

            scored.append((score, item.get('id'), name))

        scored.sort(key=lambda entry: entry[0], reverse=True)

        return scored

    def search_playlists_by_keyword(self, term: str, limit: int = 5) -> list:
        """Search playlist names and descriptions for a mood/genre/keyword
        match, for discovery-style requests (e.g. "find me an upbeat
        playlist") rather than requests for a specific playlist by name.

        :param str term: The keyword(s)/mood to search for
        :param int limit: Maximum number of candidates to return
        :return: A list of (score, id, name) tuples sorted by score descending, best first
        :rtype: list
        """

        self.logger.debug('In function search_playlists_by_keyword()')

        query_words = [word for word in self._normalize_words(term) if word not in self._KEYWORD_STOPWORDS]

        if not query_words:
            return []

        scored = []

        for item in self.get_all_playlists():
            name = item.get('name')
            # AudioMuse auto-generated playlists carry a "_automatic" suffix
            # that's not part of the descriptive text, and Navidrome's
            # comment field (when present) holds a human-written blurb
            # describing the playlist's mood/genre.
            corpus_words = self._normalize_words(name.replace('_automatic', '') + ' ' + (item.get('comment') or ''))

            word_hits = sum(1 for query_word in query_words if any(query_word in corpus_word for corpus_word in corpus_words))
            score = word_hits / len(query_words)

            if score > 0:
                scored.append((score, item.get('id'), name))

        scored.sort(key=lambda entry: entry[0], reverse=True)

        return scored[:limit]

    def search_artist(self, term: str) -> Union[dict, None]:
        """Search the media server for the given artist

        :param str term: The name of the artist
        :return: A dictionary of artists or None if no results are found
        :rtype: dict | None
        """

        self.logger.debug('In function search_artist()')

        result_dict = self.conn.search3(term)

        if len(result_dict['searchResult3']) > 0:
            # Results found
            result_count = len(result_dict['searchResult3']['artist'])

            self.logger.debug(f'Searching artists for term: {term} found {result_count} entries.')

            if result_count > 0:
                # Results were found
                return result_dict['searchResult3']['artist']

        # No results were found
        return None

    def search_album(self, term: str) -> Union[dict, None]:
        """Search the media server for the given album

        :param str term: The name of the album
        :return: A dictionary of albums or None if no results are found
        :rtype: dict | None
        """

        self.logger.debug('In function search_album()')

        result_dict = self.conn.search3(term)

        if len(result_dict['searchResult3']) > 0:
            # Results found
            result_count = len(result_dict['searchResult3']['album'])

            self.logger.debug(f'Searching albums for term: {term} found {result_count} entries.')

            if result_count > 0:
                # Results were found
                return result_dict['searchResult3']['album']

        # No results were found
        return None

    def search_song(self, term: str) -> Union[dict, None]:
        """Search the media server for the given song

        :param str term: The name of the song
        :return: A dictionary of songs or None if no results are found
        :rtype: dict | None
        """

        self.logger.debug('In function search_song()')

        result_dict = self.conn.search3(term)

        if len(result_dict['searchResult3']) > 0:
            # Results found
            result_count = len(result_dict['searchResult3']['song'])

            self.logger.debug(f'Searching songs for term: {term}, found {result_count} entries.')

            if result_count > 0:
                # Results were found
                return result_dict['searchResult3']['song']

        # No results were found
        return None

    def albums_by_artist(self, id: str) -> 'list[dict]':
        """Get the albums for a given artist

        :param str id: The artist ID
        :return: A list of albums
        :rtype: list of dict
        """

        self.logger.debug('In function albums_by_artist()')

        result_dict = self.conn.getArtist(id)
        album_list = result_dict['artist'].get('album')

        # Shuffle the album list to keep generic requests fresh
        random.shuffle(album_list)

        return album_list

    def build_song_list_from_albums(self, albums: 'list[dict]', length: int) -> list:
        """Get a list of songs from given albums

        Build a list of songs from the given albums, keep adding tracks
        until song_count is greater than of equal to length

        :param list[dict] albums: A list of dictionaries containing album information
        :param int length: The minimum number of songs that should be returned, if -1 there is no limit
        :return: A list of song IDs
        :rtype: list
        """

        self.logger.debug('In function build_song_list_from_albums()')

        song_id_list = []

        if length != -1:
            song_count = 0
            album_id_list = []

            # The list of songs should be limited by length
            for album in albums:
                if song_count < int(length):
                    # We need more songs
                    album_id_list.append(album.get('id'))
                    song_count = song_count + album.get('songCount')
                else:
                    # We have enough songs, stop iterating
                    break
        else:
            # The list of songs should not be limited
            album_id_list = [album.get('id') for album in albums]

        # Get a song listing for each album
        for album_id in album_id_list:
            album_details = self.conn.getAlbum(album_id)

            for song_detail in album_details['album']['song']:
                # Capture the song ID
                song_id_list.append(song_detail.get('id'))

        return song_id_list

    def build_song_list_from_playlist(self, id: str) -> list:
        """Build a list of songs from a given playlist

        :param str id: The playlist ID
        :return: A list of song IDs
        :rtype: list
        """

        self.logger.debug('In function build_song_list_from_playlist()')

        song_id_list = []
        playlist_details = self.conn.getPlaylist(id)

        song_id_list = [song_detail.get('id') for song_detail in playlist_details.get('playlist').get('entry')]

        return song_id_list

    def build_song_list_from_favourites(self) -> Union[list, None]:
        """Build a shuffled list favourite songs

        :return: A list of song IDs or None if no favourite tracks are found.
        :rtype: list | None
        """

        self.logger.debug('In function build_song_list_from_favourites()')

        favourite_songs = self.conn.getStarred2().get('starred2').get('song') or []

        if len(favourite_songs) > 0:
            song_id_list = [song.get('id') for song in favourite_songs]

            return song_id_list

        else:
            return None

    def build_song_list_from_genre(self, genre: str, count: int) -> Union[list, None]:
        """Build a shuffled list songs of songs from the given genre.

        :param str genre: The genre, acceptable values are with the getGenres Subsonic API call.
        :param int count: The number of songs to return
        :return: A list of song IDs or None if no tracks are found.
        :rtype: list | None
        """

        self.logger.debug('In function build_song_list_from_genre()')

        # Note the use of title() to capitalise the first letter of each word in the genre
        # without this the genres do not match the strings returned by the API.
        self.logger.debug(f'Searching for {genre.title()} music')
        songs_from_genre = self.conn.getSongsByGenre(genre.title(), count).get('songsByGenre').get('song')

        if len(songs_from_genre) > 0:
            song_id_list = [song.get('id') for song in songs_from_genre]

            return song_id_list

        else:
            return None

    def build_random_song_list(self, count: int) -> Union[list, None]:
        """Build a shuffled list of random songs

        :param int count: The number of songs to return
        :return: A list of song IDs or None if no tracks are found.
        :rtype: list | None
        """

        self.logger.debug('In function build_random_song_list()')
        random_songs = self.conn.getRandomSongs(count).get('randomSongs').get('song')

        if len(random_songs) > 0:
            song_id_list = [song.get('id') for song in random_songs]

            return song_id_list

        else:
            return None

    def get_song_details(self, id: str) -> dict:
        """Get details about a given song ID

        :param str id: A song ID
        :return: A dictionary of details about the given song.
        :rtype: dict
        """

        self.logger.debug('In function get_song_details()')

        song_details = self.conn.getSong(id)

        return song_details

    def get_song_uri(self, id: str) -> str:
        """Create a URI for a given song

        Creates a URI for the song represented by the given ID.  Authentication details are
        embedded in the URI

        :param str id: A song ID
        :return: A properly formatted URI
        :rtype: str
        """

        self.logger.debug('In function get_song_uri()')

        salt = secrets.token_hex(16)
        auth_token = md5(self.passwd.encode() + salt.encode())

        # This creates a multiline f string, uri contains a single line with both
        # f strings.
        uri = (
            f'{self.server_url}:{self.port}{self.api_location}/stream.view?f=json&v={self.api_version}&c=AskNavidrome&u='
            f'{self.user}&s={salt}&t={auth_token.hexdigest()}&id={id}'
        )

        return uri

    def get_cover_art_uri(self, cover_art_id: str) -> str:
        """Create a URI for a given cover art ID

        Creates a URI for the cover art image represented by the given ID.  Authentication
        details are embedded in the URI, same pattern as get_song_uri().

        :param str cover_art_id: A cover art ID, e.g. from a song's 'coverArt' field
        :return: A properly formatted URI
        :rtype: str
        """

        self.logger.debug('In function get_cover_art_uri()')

        salt = secrets.token_hex(16)
        auth_token = md5(self.passwd.encode() + salt.encode())

        uri = (
            f'{self.server_url}:{self.port}{self.api_location}/getCoverArt.view?f=json&v={self.api_version}&c=AskNavidrome&u='
            f'{self.user}&s={salt}&t={auth_token.hexdigest()}&id={quote(cover_art_id)}'
        )

        return uri

    def star_entry(self, id: str, mode: str) -> None:
        """Add a star to the given entity

        :param str id: The Navidrome ID of the entity.
        :param str mode: The type of entity, must be song, artist or album
        :return: None.
        """

        # Convert id to list
        id_list = [id]

        if mode == 'song':
            self.conn.star(id_list, None, None)

            return None
        elif mode == 'album':
            self.conn.star(None, id_list, None)

            return None
        elif mode == 'artist':
            self.conn.star(None, None, id_list)

            return None

    def unstar_entry(self, id: str, mode: str) -> None:
        """Remove a star from the given entity

        :param str id: The Navidrome ID of the entity.
        :param str mode: The type of entity, must be song, artist or album
        :return: None.
        """

        # Convert id to list
        id_list = [id]

        if mode == 'song':
            self.conn.unstar(id_list, None, None)

            return None
        elif mode == 'album':
            self.conn.unstar(None, id_list, None)

            return None
        elif mode == 'artist':
            self.conn.unstar(None, None, id_list)

            return None
