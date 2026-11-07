# Stream Demote

A Dispatcharr plugin that moves streams that fail for real viewers down the channel's
stream order, so the next tune-in starts on one that works.

Watching uses none of your provider's connections: it only reads what viewers have already
started. Moving the order uses none either, and the order is Dispatcharr's own, so it stays
even if you remove the plugin. The one thing that opens a stream is moving a viewer who's
still there to the next stream, when a flag or a content check asks for it. See
[Connections](#connections).

Needs Dispatcharr 0.33.0. It has been tested on 0.33.0 only, with a fake provider. See
[What has been tested](#what-has-been-tested).

## What it does

- **Watches failovers.** When Dispatcharr gives up on a stream and moves to the next, that
  stream gets a strike.
- **Demotes.** Enough strikes, and once another stream on the channel has shown it can play,
  the stream moves to the bottom of the channel's order, above any custom stream such as a
  fallback card.
- **Holds back when it isn't the stream's fault.** See [Not over-reacting](#not-over-reacting).
- **Gives streams a way back.** See [Earning its way back](#earning-its-way-back).
- **Keeps demotions when other tools re-sort.** See [Other tools](#other-tools).
- **Lets a person flag a stream.** A button in the player, or anything else that can send
  a POST, can report a bad picture.
- **Can check the picture and sound.** Off by default. See [Content checks](#content-checks).

### Moving on to the next stream

**The automatic move for a stream that fails or stalls is Dispatcharr's built-in failover, not
this plugin.** When a stream fails, Dispatcharr moves the viewer on by itself, and this plugin only
remembers the result in the order. It can't make that faster: a provider error takes about a
second to detect, no data about 10 seconds, and a stall after playing about 25.

This plugin forces a move to the next stream in two cases: when someone flags a stream, and when
a content check has found a lasting problem. Both go to the next stream in order, and skip one that
has just failed or is already demoted.

Dispatcharr doesn't notice a stream that sends rubbish, or a black picture. In proxy mode, random
bytes play on with no event at all. Only a flag or a content check can catch a picture problem, and
random bytes that ffmpeg can't read give a content check no answer, so only a flag catches those.

## Install

1. In Dispatcharr, open Plugins, Import Plugin, and choose the release zip. Or copy this
   folder to `/data/plugins/stream_demote`, and press the refresh button on the Plugins page.
2. Enable it.
3. **Restart the Dispatcharr container.** Demotion works without a restart, but the flag route,
   the background check and the way it sees hand edits only reach every web worker after one. The
   Status button says whether they're live.
4. Leave **Dry run** on for a few days. Press **Export journal** to see what it would have
   done, then turn it off.

It keeps its data in its own database tables, named `plugin_stream_demote_*`. They're
included in Dispatcharr's backups, and you don't need to set anything up.

## Settings

Every value below is a starting point, not a measurement. They haven't been tuned against
a real provider.

**When a stream is demoted**

| Setting | Default | Meaning |
|---|---|---|
| Dry run | on | Log what it would do, change nothing. That covers restores and putting streams back after another tool too. A value that can't be read counts as on. The Undo buttons are a person asking, so they still work |
| Score that demotes | 1 | Raise to 2 to need two hard failures |
| Score window | 24 hours | Older failures stop counting |
| Weight: hard failure | 1 | Dispatcharr's retries ran out (`max_retries_exceeded`) in the early period |
| Weight: no data or early stall | 1 | `health_monitor` in the early period |
| Weight: late failure | 0.5 | A stall or hard failure after it. One blip in six hours isn't a bad stream |
| Weight: too slow | 0.5 | `buffering_timeout`. FFmpeg profiles only |
| Early period | 5 minutes | |
| Hold before acting | 45 seconds | Wait, so an outage can show itself first |
| Another stream must play | 10 seconds | A stream only drops once another on the channel has delivered data for this long |
| Wait for that proof | 5 minutes | How long a running channel can take to show it. The strike stays after that |

**Not over-reacting**

| Setting | Default | Meaning |
|---|---|---|
| Proof a provider is up | 30 seconds | Another stream on the account that keeps playing this long |
| Outage: failed streams | 3 | On at least two channels, with nothing else playing there |
| Outage: window | 120 seconds | |
| Outage: pause | 30 minutes | No demotions on that account for this long |
| Outage: give up after | 24 hours | After this the account counts as broken, and demotions go ahead. 0 means never |
| Outage: undo recent | 10 minutes | Demotions this recent on that account are reversed |
| Follow-on failure | 30 seconds | See below |
| Stall guard | 35 seconds | See below |
| Zap guard | 10 seconds | See below |
| Let off at most | 3 spells | See below |
| All streams failed | 60 minutes | If every stream failed in this time, the order is left alone |
| Limits | 3 per channel in 5 minutes, 6 per channel a day, 20 an hour in all | Over the last one it stops and warns you in Dispatcharr's notifications |
| Channels to leave alone | none | Comma-separated channel numbers. Nothing moves their streams: no demotion, no second chance, no earning back, no putting back after another tool, and no content checks |

**Earning its way back**

| Setting | Default | Meaning |
|---|---|---|
| Earn back by playing | 10 minutes | A clean play this long returns it to its old place |
| Second chance after | 72 hours | 0 means never. Doubles after each repeat failure, up to 30 days |

**Other tools that sort streams**

| Setting | Default | Meaning |
|---|---|---|
| Keep demotions when other tools re-sort | on | See [Other tools](#other-tools) |
| Check every | 60 seconds | Only channels with an active demotion, in one query |
| Wait for it to settle | 10 seconds | A changed order must hold still this long first |
| Most times per channel per hour | 6 | Above this it stops on that channel for the hour and warns you |
| Forget a removed stream after | 10 minutes | A demoted stream that has left the channel for this long is forgotten |

**Content checks** are in [their own section](#content-checks).

**Flag button**

| Setting | Default | Meaning |
|---|---|---|
| Move to the next stream on a flag | on | |
| Flag limit: per channel | 60 seconds | One flag per channel in this time |
| Flag limit: per token | 6 an hour | Counts every call the token makes. See the note on gateways below |
| Token label | | Name for a new token, or the label to revoke |

Dispatcharr's Plugins page also has **Write a shared event**, on by default, and **Keep the
journal**, 90 days.

## Not over-reacting

- **Provider-wide outage.** Each failure waits 45 seconds before it's judged. If 3 or more
  streams on one account failed on at least 2 channels, and nothing else on that account has
  kept playing, it counts as an outage. Nothing moves, that account is paused for 30 minutes,
  and recent demotions on it are undone. If another stream on the account has played on
  through that time, the provider is plainly up, so the failures count as the streams' own.
- **Two failures together, with no proof either way.** If a second stream on the account fails
  within 30 seconds, and nothing proves the provider is up, nothing moves the first time.
  If those same streams fail again later, they're demoted. This is how a channel with several
  dead streams from one provider still gets sorted out.
- **A viewer zapping.** Leaving a working stream produces no failure, so it can't count against
  it. A stream tried within 10 seconds of another channel on the same account stopping isn't blamed
  if it fails, because the provider may not have seen the old connection close. That goes by when
  the attempt began, because a slow refusal can take 20 seconds to show, and it applies whether or
  not you've set a limit in Dispatcharr, because the provider may have one of its own.
- **After a stall.** Dispatcharr can leave a stalled connection open for up to 30 seconds after
  a failover, so the provider sees one too many and may refuse the next stream. Failures on streams
  tried within 35 seconds of a stall on that account don't count. A flag on a stalled stream counts
  as a stall too.
- **Let off too often.** A dead stream only ever tried straight after a zap would be let off for
  good. After 3 separate spells, at least 10 minutes apart, under those two guards, its failure counts.
- **A stream that came back.** If the stream that failed is playing and delivering data by the time
  the failure is judged, the failure is dropped.
- **Every stream on a channel failing.** That's more likely the channel than its streams, so
  the order stays as it was. Dispatcharr doesn't send a failure for the last stream on a channel when
  it simply delivers nothing, so a stream also only drops once another on the channel has
  delivered data for 10 seconds. A channel that's off the air never gets there, and isn't shuffled.
- **Another tool probing.** IPTV Checker, and Stream-Mapparr's throughput probe, open provider
  connections Dispatcharr doesn't count, so a viewer tuning in during one can be refused. Those
  failures don't count. See [IPTV Checker](#iptv-checker).

**What can still go wrong.** One viewer on one channel during an outage looks exactly like one
bad stream, so that stream is demoted. It goes back if more failures show the outage within
10 minutes, or when it earns its way back. A provider that stays down for over 24 hours
counts as broken and its streams get demoted, which is usually what you'd want. A stream a viewer
leaves within 10 seconds of the failover gets a strike but no demotion, because nothing showed
another stream could play.

## Earning its way back

- **By playing.** If viewers land on a demoted stream, because the ones above it failed or are
  full, and it plays for 10 minutes without failing, it returns to its old place. If it was demoted
  by a content check and those are on, it also needs two good samples.
- **By a second chance.** After 72 hours it goes back to its old place anyway. If it fails,
  even with weak evidence, it's demoted again and the wait doubles. So a dead stream costs a
  viewer one slow start every so often, and never more than that. This is the only way a stream
  nobody lands on can be retried without opening it.

## Undo

Every move is in the journal. **Export journal** writes it to
`/data/plugin_reports/stream-demote/journal.csv`.

- **Undo the last demotion**
- **Undo a channel**, using the number in Channel to undo
- **Undo everything**

Undo moves each stream that's still demoted back to its old place, newest first. A stream that has
already come back, or that you moved by hand, is left alone. Undoing also clears that stream's strikes,
and an Undo sticks: a later re-sort by another tool doesn't bring the demotion back. A later failure can.

Removing the plugin doesn't change any order. If you remove it and put it back, its tokens stop
working and its demotions are forgotten, so old state can't come back with it. The journal stays. To
remove its data as well, press **Erase plugin data** first, then remove the plugin, then, if you want
the empty tables gone, run:

```sql
DROP TABLE plugin_stream_demote_failure, plugin_stream_demote_session, plugin_stream_demote_demoted,
  plugin_stream_demote_journal, plugin_stream_demote_token, plugin_stream_demote_rate,
  plugin_stream_demote_account, plugin_stream_demote_guard, plugin_stream_demote_sample,
  plugin_stream_demote_hash, plugin_stream_demote_meta;
```

Copying a folder out by hand, with no use of the Plugins page, leaves Dispatcharr's record in place,
so it can't tell that from a restart. Press Erase plugin data first in that case.

## Other tools

Stream-Mapparr and Lineuparr rewrite channels' stream lists, on a schedule or a button. Dispatcharr
sends no event for that, so without this, their next run would undo every demotion.

**How it notices.** About once a minute it compares channels with an active demotion with how it left
them, in one query. It also looks when a demoted stream is about to move for another reason. Channels
with no active demotion aren't looked at. A changed order has to hold still for 10 seconds first, so a tool
that's still working isn't raced. In testing a re-sort was put right in 13 to 88 seconds, and a viewer
tuning in during that time can land on a demoted stream.

**What it does.** Demoted streams go back to the bottom, oldest demotion first, above any custom stream.
Everything else keeps the other tool's order. The place the tool gave a demoted stream becomes the place it
goes back to when it earns its way back. Each of these is in the journal as `reapply`.

**Who did it.** It tells a person's change from a tool's by how the change was made:

| What happened | Taken as | What it does |
|---|---|---|
| The channel's rows were deleted and made again | A tool. Stream-Mapparr's Sort, Match and Fill Mirror Backups, Lineuparr's match | Puts demoted streams back |
| The order was changed through Dispatcharr's channel API, and is still what was saved | A person. Dragging in the channel editor, taking a stream out and adding it back, or your own script | Your order stays. A demoted stream you moved up is let go for good, and it's `hand_release` in the journal |
| Same rows, any other way | A tool. Lineuparr's Re-sort, or a preserving match adding streams | Puts demoted streams back |

A save through the API that leaves the order as it was, such as renaming a channel or changing its guide, doesn't
count. If a tool re-sorts a channel after you changed it, and before the plugin has looked, the tool's order is what's
there, so demoted streams go back down.

Dispatcharr has no way to say who changed something, so seeing the API takes a small wrapper around its
channel serializer, added when the plugin loads. That isn't a supported plugin feature and could break in a
future Dispatcharr. If it can't be added, Status says so, and any change that keeps the rows counts as by hand,
so the plugin leaves it alone rather than fight you. A later Dispatcharr could also save channels some other way,
which Status can't see. Your drag would then look like a tool's and be put back, so check after an upgrade.

**Limits.** No loops: what it writes becomes what it expects, so it doesn't answer its own writes. If something keeps rewriting a channel, it answers 6 times in an hour there, then stops for the rest
of that hour and warns you. In Dry run it writes `would_reapply` and moves nothing. Under **Channels to leave
alone** it does nothing at all. If you'd rather a tool's order always won, list those channels there, or turn
the setting off.

**A stream id changing.** On an account that identifies streams by URL, a changed URL makes a new stream in
Dispatcharr. Nothing links the old and new streams, so:

- The new stream starts clean. It isn't demoted, even if it replaces a demoted one. A new URL is often the
  fix, and it can fail its own way down if not.
- If the old stream stays in the channel, as Dispatcharr's own refresh leaves it, it stays demoted until
  Dispatcharr deletes it.
- Once a demoted stream has been gone from the channel for 10 minutes, it's forgotten, and the journal says
  `demotion_gone`.

The same goes for a tool that swaps streams for others. Accounts that identify streams by a provider's id,
such as XC accounts, keep their ids when a URL changes.

Nothing else overwrites the order. An M3U refresh doesn't touch it while the stream is still in the playlist.
A stale browser tab counts as a hand edit, because it is one: dragging streams there saves that tab's whole list.

## Content checks

**Experimental.** Off by default, with one switch: **Check content**. When on, a failed check is only written to the
journal, unless you also turn on **Act on content checks**. In testing, acting on them demoted good streams in ordinary
situations, listed under [Known false alarms](#known-false-alarms), so leave that off and read the journal instead. They run only on a stream a viewer already has open, on what
Dispatcharr already holds, so they use no provider connection and open nothing. They look at the same things
IPTV Checker looks at when it opens a stream itself: a black picture, a frozen picture, silence, and placeholders.

**How.** A background check copies a few seconds of the channel's buffer out of Redis to a temporary file, and
runs ffmpeg on that file, with only local files allowed. It keeps a small fingerprint of the picture, and throws
the file away. No picture, sound or address is stored: only 64-bit fingerprints, as numbers.

| What | How it's decided |
|---|---|
| Black picture | `blackdetect`, with at least 98% of each frame black, for 90% of the sample |
| Frozen picture | `freezedetect` at -60 dB, for at least 5 seconds, and still frozen at the end of the sample |
| Silence | `silencedetect` and `volumedetect`, both at or under -70 dB. Digital silence measures -91 dB, and the quietest real channel IPTV Checker measured was -44 dB |
| No audio track | No audio stream, or no audio in it. Video with no audio |
| Placeholder picture | The same still picture on streams of 3 different channels, or one you taught it |
| Looping clip | The same run of different pictures, seen again at least 3 times over 2 minutes |
| Fixed-length file | Only on FFmpeg-style profiles, which record the input's container: an MP4, MKV or AVI isn't live. A proxy profile can't see this, and a file that plays through and restarts shows up as looping instead |
| Radio | No video track at all. **Skipped**, as IPTV Checker treats it, and not sampled again that session |

**So that a real dark scene or a quiet moment doesn't count:**

- One sample never counts. It takes **3 bad samples in a row over at least 2 minutes**, with a sample every 45 seconds.
- A black or frozen picture with sound still playing could be a dark scene, or a radio channel showing a card, so it
  takes **twice** that. Turn off **Count a still picture when there is sound** to never count it.
- A still only counts as seen on a channel if it holds through 3 samples in a row, over at least a minute, and a flat
  picture never does, so a programme's own black frames and passing slides teach nothing.
- A talking head doesn't count as looping: the frames have to show several different pictures, in the same order.
- If every stream on a channel fails, nothing is demoted. The viewer stops being moved.
- Anything it can't read gives no answer, and no answer does nothing: Redis failing, a chunk expiring mid-read,
  the stream changing mid-read, a sample that ffmpeg can't decode or that holds too little picture, or a run past the time limit.

**When a check fails.** It's written to the journal as `content_failed`, with the cause. With **Act on content checks**
on, it also counts like any other failure, as `content_black`, `content_silence` and so on. The viewer
moves to the next stream, if they're still there, and only within the limits. Then, if the next stream gives a good
sample, the failed one is demoted. If the viewer couldn't be moved there's nothing to compare with, so it's
demoted on this evidence. All the usual limits apply, and in Dry run it writes `would_rotate` and `would_demote`
and moves nothing.

**Teaching a placeholder.** Press **Teach a placeholder** with a channel in **Teach from channel** while someone's
watching. A taught picture counts on every channel, so only teach a true placeholder. A flag never teaches.
**Forget placeholders** clears them all.

### Known false alarms

In a second round of tests, with **Act on content checks** on and default settings, these demoted a good stream:

- A sports channel showing a still slate before kick-off, silent or with music, where every stream carried the slate.
- One news programme, a studio shot with a ticker, carried by 3 channels. It was learned as a shared placeholder.
- A near-black scene with quiet talk lasting over about 4 minutes.
- A programme with the same advert in every break, and one ordinary synthetic programme, both taken as looping.

All the video was synthetic, so real channels may do better or worse. A news ticker over a still picture, a slideshow
with music, a quiet talk and near-silent hum didn't trigger. A placeholder with a slowly moving logo wasn't caught.

**What it costs.** One check at a time across all workers, at low priority, one thread, with a 20 second limit, and none
if the machine is already busy. A channel with only one stream, a custom stream, or a channel to leave alone isn't
sampled, because nothing could be done about it. See [What has been tested](#what-has-been-tested) for what it used.

**How long it takes.** In testing a verdict came about 2.5 to 3.5 minutes after tuning in, 5 to 7 with sound still playing, and
longer the first time a placeholder is learned. That's the price of not acting on a dark scene. For a stream you can see is bad,
the flag is faster.

| Setting | Default | Meaning |
|---|---|---|
| Check content | off | The off switch for all of this |
| Black picture, Frozen picture, Silence, No audio track, Placeholder pictures and files, Looping clips | on | Each one alone |
| Count a still picture when there is sound | on | See above |
| Act on content checks (experimental) | off | Off: failed checks are only written to the journal. See [Known false alarms](#known-false-alarms) |
| Move the viewer on | on | |
| Sample length | 6 seconds | It copies 4 seconds more, to be sure of a full picture |
| First sample after | 20 seconds | |
| Sample every | 45 seconds | |
| Bad samples needed | 3 | Never fewer than 2 |
| Over at least | 120 seconds | Never less than 30 |
| Most checks at once | 1 | |
| Give up on a check after | 20 seconds | |
| Most moves per hour, everywhere | 6 | And 3 per channel |
| Placeholder: seen on this many channels | 3 | |
| Weight: failed content check | 1 | |

**What it can't do.** It only knows what viewers open. A stream at a very low bitrate may never get that far: Dispatcharr fills a
buffer before it starts a viewer, and in testing a 77 kbit/s black picture gave the viewer nothing and no event, so nothing was sampled. It reads the input as MPEG-TS, which is what Dispatcharr's proxy and
FFmpeg-style profiles carry, and gives no answer for anything else. A loop longer than a few minutes is unlikely to be seen, and
one over 15 minutes won't be. Only the first audio track is checked. A sample goes back no more than about 32 MB, so at very high
bitrates it covers less time, and may give no answer. It has only run against synthetic video, so these thresholds come from
IPTV Checker's measurements, not from mine on real channels.

## IPTV Checker

They work well side by side, and IPTV Checker can stay installed and be run by hand.

- **Its checks don't count here.** It runs ffprobe straight at the provider, not through Dispatcharr's proxy, so no
  viewer's session and no event is involved. A dead result in its report means nothing to this plugin.
- **A viewer tuning in during its check** can be refused, because its connections use provider seats Dispatcharr doesn't
  know about. While its progress file says it's running, failures don't count against a stream.
- **Its results don't reach this plugin.** It writes each stream's `stream_stats`, and clears them on a dead stream.
  This plugin doesn't use them, so it won't be misled by old or cleared values.
- **Renaming and moving channels** changes nothing here. **Deleting a dead channel** drops its demotions with it.
- **Trigger Stream-Mapparr After Scheduled Check** runs a sort, which is handled like any other. See [Other tools](#other-tools).
- **The difference.** It's for a sweep, by hand, of everything, and it uses seats. This plugin's content checks only see what
  viewers open, and use none. So it can find a dead channel nobody watches, and this can find one going bad in the moment. Its own
  black, frozen and silent checks are worth keeping off if your provider's limit is tight, because each one opens a stream.

## Flag API

```
POST /api/stream-demote/flag/
Authorization: Bearer <token>
Content-Type: application/json

{"channel_number": "101"}
```

`channel_uuid` works in place of `channel_number`. You can only name a channel, never a stream: the server looks up
what that channel is playing right now. That rules out naming a stream you can't see. A channel that nobody is
watching gets `not_playing`, even if it's still running through a shutdown delay.

| Reply | Status | Meaning |
|---|---|---|
| `rotated` | 200 | Moved down, and the session moved to the next stream |
| `demoted` | 200 | Moved down. Nothing else could take the session, or the switch failed |
| `observed` | 200 | Dry run. Logged only |
| `noted` | 200 | Logged. There's no other stream to move to |
| `channel_problem` | 200 | Every stream was flagged in the last hour, so nothing changed |
| `paused` | 200 | A limit was reached, or another writer was busy on that channel, so nothing changed |
| `not_playing` | 409 | No viewer on that channel |
| `ambiguous` | 409 | More than one running channel has that number |
| `rate_limited` | 429 | See the `Retry-After` header |
| `unauthorized` | 401 | Missing, wrong or revoked token |
| `forbidden` | 403 | Your address isn't on Dispatcharr's Stream Endpoints list |
| `bad_request` | 400 | |

Replies never name a stream, a URL or a provider.

### Tokens

Set **Token label**, for example `living-room-tv`, and press **Create a flag token**. The token
shows once, under Output. Only its SHA-256 hash is stored. Press **Revoke a token** with the same
label to end it.

A token can do one thing: flag a channel someone's watching, within the limits above. A flag never teaches a
placeholder.
Dispatcharr's own authentication doesn't recognise it, so it opens no other route, and this
route accepts nothing else. An admin key or an admin session gets a 401 here. The plugin never
asks for, stores or uses an admin credential, and neither does the button.

Send it only in the `Authorization` header. A token in the URL is refused, and it would also end up
in Dispatcharr's request log, which records full URLs.

The route is added to Django's URL list when the plugin loads, because plugins have no routes of
their own. That isn't a supported plugin feature and could break in a future Dispatcharr. If it
does, demotion carries on and only the flag stops. It stays in memory until a restart, so it
checks on every call that the plugin is still enabled. Dispatcharr doesn't apply its Network Access lists
to a plugin's route by itself, so the route checks the **Stream Endpoints** list, the same as the streams.

## The Jellyfin button

[`examples/jellyfin-button.js`](examples/jellyfin-button.js) adds a button to the Jellyfin web
player's controls while live TV is playing. It's written for older TV browsers: nothing newer
than ES2018. It's an example, and has only been run against a fake page, not a real Jellyfin.

It reads the channel number from Jellyfin's session, so your Dispatcharr M3U output needs to
carry channel numbers (`tvg-chno`), and that number must be the channel's number in Dispatcharr.

### Using the button safely

**Never put a token in the script.** Scripts injected into Jellyfin, through the JavaScript
Injector plugin or a reverse proxy, are usually served to anyone who can load the page, before
they sign in.

**Never use an admin key or the plugin `/run/` route from a browser.** Anyone with the browser's
developer tools can read it, and it opens everything.

Two ways, safest first:

**1. A gateway beside Jellyfin.** The browser sends its normal Jellyfin session to a path on
Jellyfin's own address. The web server checks that session with Jellyfin, and adds the flag token
itself. No secret reaches the browser, and signing out ends access. Set `useGateway: true` and
`flagUrl: '/flag-stream'` in the script. For nginx:

```nginx
location = /flag-stream {
    limit_except POST { deny all; }
    auth_request /flag-stream-check;
    proxy_set_header Authorization "Bearer sdf_your_token_here";
    proxy_pass https://dispatcharr.example.com/api/stream-demote/flag/;
    # The token goes over this connection, so check who's on the other end.
    proxy_ssl_server_name on;
    proxy_ssl_verify on;
    proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
}

location = /flag-stream-check {
    internal;
    proxy_pass http://127.0.0.1:8096/Users/Me;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Authorization $http_authorization;
}
```

The whole household shares this one token, so the limit of 6 an hour is shared too. Raise **Flag limit: per token**
to suit. The limit of one flag per channel per minute still holds, and the button says how long to wait.

**2. A token on each device.** The script asks for a token the first time, and keeps it in that
browser's `localStorage`. Use one token per device, with a label that says which. If one leaks,
the worst it can do is flag channels someone's watching, 6 times an hour, and you can see
and reverse that in the journal and revoke it by label. Use HTTPS: the script refuses to send a
token over plain HTTP. To clear a device, run `StreamDemoteFlag.forgetToken()` in its console.

The route answers cross-origin requests, as Dispatcharr's API does, so the button can call it
from Jellyfin's address.

## Connections

| What | Provider connections it uses |
|---|---|
| Watching events, reading the order, changing the order, Undo, content checks | None |
| A flag or a failed content check with a viewer there | Moves theirs: Dispatcharr closes the old stream and opens the next |
| A flag or content check with nobody watching | Nothing happens |

**A move isn't always clean.** If the old stream is healthy, it closes first. If it has stalled, which is the main reason to
flag one, Dispatcharr can hold the old connection open for up to 30 seconds more while the new one opens, so the provider
briefly sees two. On an account with one connection, the next stream can then be refused. So a flag on a stalled stream tries a
stream on another account first, and a refusal in that time isn't held against the stream refused. If every other stream is on
the same one-connection account, there's nowhere else to go: the provider refuses the new stream until the stalled one closes,
the viewer can lose the channel, and the reply still says `rotated`. Dispatcharr alone tends to wait it out instead.

Set each M3U account's maximum streams in Dispatcharr to the number the provider really allows. Left at 0, Dispatcharr
treats it as unlimited, and a move to "another account" can land on a line someone else is using.

## Limits

- **It only knows about streams viewers try.** A stream nobody starts is never judged. That's the
  price of using no connections.
- **Provider-full skips can't be seen.** Dispatcharr skips a stream on a full account with no
  event. They can't count for or against a stream.
- **Redirect profiles can't be seen.** The viewer goes straight to the provider, so Dispatcharr
  sends no events. The same goes for clients using provider URLs directly.
- **A switch isn't seamless.** The new stream starts a moment later, and the viewer's
  player may glitch at the join. Live TV has no position to lose.
- **Each channel is separate.** A stream in two channels is judged separately in each.
- **A channel that wedges during start-up sends no events**, so no failure is recorded. Having no
  failures doesn't prove a stream is healthy.
- **A channel already playing when the plugin starts** isn't known to it until its next event, so it isn't content-checked until then.
- **Dispatcharr 0.34 will change where event handlers run.** The code doesn't rely on running
  inside the proxy process, and a repeated event counts once, but 0.34 hasn't been tested. Its plugin
  permissions may also limit starting a program, so content checks may need one declared there.
- **PostgreSQL is what was tested.** The SQL also runs on SQLite in the unit tests. A Dispatcharr
  running on SQLite has not been tried.

## Stats

To see which providers are worth keeping, try the separate Stream Stats plugin. Neither plugin
needs the other, and neither imports the other. When this plugin moves a stream it writes a
`stream_demoted` row to Dispatcharr's System Events, with no URLs in it, which a stats plugin can
read. Turn that off with **Write a shared event**. Putting a stream back after another tool doesn't write one.

## What has been tested

On a throwaway Dispatcharr 0.33.0 on an isolated network, with a fake provider that enforces connection limits and counts
every connection, and test viewers that read the stream but don't play it. Each container had one CPU. The numbers
below come from those runs, not from real use.

**Connections.** The same viewers ran with no plugin, in dry run, live, and with content checks on in dry run and live. Each time the
provider saw the same 6 connections and 3 failed tries, stream by stream, with the same peak on the account and no probes. Content checks took
samples during those runs. With nothing playing, for 75 seconds, it saw none. Once the bad streams were demoted, the same viewers needed 5
connections and no failed tries.

**Bad starts and false alarms.** Provider errors, no data, a stream that keeps dying, a stall soon after starting, and a slow stream on an FFmpeg
profile were each demoted, the slow one only after a second failure. A stall after 30 seconds of good play makes Dispatcharr reconnect to the same stream, so
that isn't a failure unless the reconnect fails too. Every stream hanging or silent on a channel moved nothing. A stream that failed for 3 seconds and came back wasn't demoted while it played. A hidden limit at the
provider, a refusal taking 5 seconds three times over, and a channel stopping just after the next one started each got no stream blamed. A flag on a stream that had
just frozen, on an account with one connection, went to another account first and blamed nobody. A flag after the viewer left, with a shutdown delay
set, did nothing and opened nothing. Dry run, Channels to leave alone and a hand order all held against a second chance. An outage rolled back the
one demotion it should and moved nothing else. Flags racing Dispatcharr's channel editor, far faster than any real use, gave no failures
and no deadlocks.

**Other tools.** The real Stream-Mapparr 1.26.3052124 and Lineuparr 1.26.3052143, on the same instance:

- Stream-Mapparr's Sort put a demoted stream back on top, by deleting and remaking the rows. Lineuparr's match did the same, and its Re-sort did it in place. Each time the
  demoted stream was back at the bottom in 13 to 88 seconds, and the rest kept the tool's order. Each run caused one write, with no further writes over the next two minutes, and no
  provider connections.
- A hand reorder stuck through later Sorts, by both Dispatcharr routes. An Undo stuck through a later Sort. Channels to leave alone kept the tool's order, and caught up when taken off the list. Dry run only wrote it down, and caught up when turned off.
- A changed URL made a new stream, and the old one stayed demoted while it stayed in the channel. Once deleted, its demotion was forgotten and the new one wasn't touched.
- With no events at all, after a restart, a rewrite was noticed in about 75 seconds.
- The periodic check took about 3 ms with 10 active channels, 5 ms with 100, and 30 ms with 1000.

**IPTV Checker.** The real 1.26.3052213 ran by hand beside it, with its stream check, rename, move and delete. A failure during its check
didn't count and one after it did. Its results and its writes to `stream_stats` changed no demotion, and deleting a dead channel dropped that channel's demotion.

**Content checks.** On real ffmpeg with video generated for the purpose, through Dispatcharr's own buffer, with default settings:

| Stream | Result | Tune-in to verdict |
|---|---|---|
| Black and silent | `content_black`, viewer moved, then demoted | 169 s, demoted at 214 s |
| Frozen and silent | `content_frozen` | 145 s |
| Moving with digital silence | `content_silence` | 160 s |
| Moving with no audio track | `content_no_audio` | 206 s, sharing checks with three other channels |
| Frozen with sound | `content_frozen` | 404 s |
| One slate, encoded four ways, on four channels at once | `content_placeholder` | 372 to 448 s |
| The same slate on a later channel | `content_placeholder` | 141 s |
| A 24 second clip going round | `content_looping` | 292 s |
| An MP4 on an FFmpeg-style profile | `content_file` | 164 s |
| Every stream on a channel black | Viewer moved twice, then left. Nothing demoted | |
| **A 150 second near-black scene over quiet talk, then a 15 second blackout with silence** | **Nothing.** Seen as dark 3 times in a row, and let be | |
| **An ordinary programme** | **Nothing** in this run. A later run had false alarms, see [Known false alarms](#known-false-alarms) | |
| **Radio with no video** | **Skipped**, after one sample. On an FFmpeg-style profile, not sampled at all | |

In those runs:

- Each stream opened once, and the one it moved to opened once, with the viewer's connection open throughout. In dry run only the first opened.
- **CPU:** one check took about 0.2 CPU seconds at 640x360, and 1.1 at 1080p and 6 Mbit/s H.264. At one check every 45 seconds, that's about 2.4% of a core for a 1080p channel. Never more than one ran at once,
  at the lowest priority, in either web worker, whichever owned the channel.
- **A failed read did nothing:** breaking the buffer's key names or its Redis client gave no samples and no action, and the viewer played on.

**Removing it.** Removing and restoring the plugin ended old tokens and demotions. Breaking each piece of Dispatcharr it uses in turn left viewers unaffected, and Status said when events were failing.

**Not tested:** a real provider, real channels, a real Jellyfin or TV, a real video player, HLS and fMP4 output, VLC and Streamlink profiles, XC accounts, Dispatcharr 0.34, and SQLite as Dispatcharr's own database. The button has only run
against a fake page. All video was synthetic, so a real broadcast may behave differently. The defaults are reasoned starting points, not tuned against real use.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

These need no server, no Django and no network. They use a real SQLite database and a fake
Dispatcharr. To include the button's behaviour, have `node` on your path. To parse the button with
a strict ES2018 parser, set `ACORN_PATH` to a copy of the acorn package. To run the content checks on
real video, have `ffmpeg` on your path.

One test reads the syntax tree of every shipped file and fails on anything that could open a
connection: `requests`, `urllib`, `socket`, `get_stream`, `ffprobe` and so on. Only one file may
start a program, and others hold it to ffmpeg with a forced format, and local files only.

## Licence

[MIT](LICENSE)
