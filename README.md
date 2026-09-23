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
- **Resuming a playlist picks up where you left off.** Switching from one playlist to another used to always
  restart the new one at track 1 and discard the old one's position for good. Now, asking to play a playlist
  you'd previously switched away from resumes it mid-track where you left it, instead of starting over — works
  across any number of playlists switched between, not just A/B. Only covers playlist-to-playlist switches:
  switching to an album/artist/etc. and back to the same playlist still restarts it.
- Invocation name changed to "my music".
- **Unverified requests get a 404**, not a 405/400. The skill endpoint only accepts POST requests that pass
  Alexa's signature/timestamp verification; anything else (a browser GET, a probe with no/bad signature) now
  looks like the route doesn't exist instead of confirming a live endpoint.
- **Reports "now playing" to Navidrome.** Upstream only scrobbles a track once it finishes, so Echo playback
  never appeared in Navidrome's now-playing list (`getNowPlaying`). When Alexa starts a track, the skill now sends a
  non-submission scrobble for it (no play count is recorded), so AskNavidrome sessions show up in Navidrome and
  anything built on its now-playing data.
- **`/status` endpoint** for dashboards (e.g. Home Assistant). `GET /status` returns, per Echo device, what the
  queue is playing from (e.g. "the playlist Crossover", "the album X by Y"), the current track, the next few
  queued tracks (`?upcoming=N`, default 5, max 25) and the queue length. It's disabled unless `NAVI_STATUS_TOKEN`
  is set, and requests must send `Authorization: Bearer <token>`; anything else gets a 404, like the skill
  endpoint. Since the skill URL is usually publicly routed, read this from the internal network rather than the
  public hostname. Queues are held in memory, so the list is empty after a restart until the next voice request.
- `Dockerfile` builds from the checked-out source (`COPY .`) instead of the upstream Dockerfile's `git clone` of
  itself mid-build, so a local build actually picks up these changes.
