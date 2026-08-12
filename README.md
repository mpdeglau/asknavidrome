# AskNavidrome

> This is a fork of [rosskouk/asknavidrome](https://github.com/rosskouk/asknavidrome), with the
> changes described below.

**AskNavidrome** is an Alexa skill which allows you to play music hosted on a SubSonic API compatible media server, like Navidrome.

You can stream your own music collection to your Echo devices without the restrictions you would normally face with regular 
streaming services like Amazon Music or Spotify.  AskNavidrome allows you to:

- Skip backwards and forwards in your current queue or playlist without limitation.
- Avoid paying subscription costs.
- Avoid being forced to listen to adverts at regular intervals.
- Actually use the music collection you have already paid for!
- Run the service on a PC directly or inside a Docker container.

See upstream's full documentation [here](https://rosskouk.github.io/asknavidrome) — it still covers setup, the Subsonic
config, and deploying the Alexa skill. The changes below aren't reflected there yet.

## Changes in this fork

- **Playlist discovery**: new intents to list your playlists, find one by mood/topic (`NaviSonicFindPlaylist`), and
  page through results (`NaviSonicHearMorePlaylists`), plus a `NaviSonicHelpTopic` intent for in-skill help.
- **Dynamic playlist matching**: `NaviSonicPlayPlaylist` matches against the real playlist name Alexa resolves
  (`AMAZON.SearchQuery`) instead of a hardcoded `playlist_names` slot-type list, so playlists AudioMuse generates
  on the fly are playable without editing the interaction model each time.
- **Real cover art** sent in `AudioPlayer` metadata instead of a static icon.
- **Per-device playback state**, so multiple Echo devices don't clobber each other's queue/position.
- Invocation name changed to "my music".
- **Unverified requests get a 404**, not a 405/400. The skill endpoint only accepts POST requests that pass
  Alexa's signature/timestamp verification; anything else (a browser GET, a probe with no/bad signature) now
  looks like the route doesn't exist instead of confirming a live endpoint.
- `Dockerfile` builds from the checked-out source (`COPY .`) instead of the upstream Dockerfile's `git clone` of
  itself mid-build, so a local build actually picks up these changes.
