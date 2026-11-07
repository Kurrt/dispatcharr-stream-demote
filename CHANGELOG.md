# Changelog

## 0.3.0

**Changed**

- Content checks only report by default. A failed check is written to the journal as `content_failed`, and only counts as a
  failure with the new **Act on content checks**, which is off. Testing found good streams demoted, see the README.
- A flag no longer teaches a placeholder. Only the admin button does.

**Fixed**

- Saving a channel without changing its order, such as a rename, no longer makes a later tool re-sort count as a person's
  change, which let go of the demotion.
- Taking a demoted stream out and adding it back on top, by hand, now sticks.
- A tool re-sorting a channel after a hand change, before the plugin looked, is now put right.

## 0.2.0

**New**

- Demotions survive other tools. Channels with an active demotion are compared about once a minute, and demoted streams go
  back to the bottom after a tool such as Stream-Mapparr or Lineuparr rewrites the order. A hand reorder or an Undo sticks.
- Optional content checks on streams a viewer already has open: black, frozen, silent, no audio, placeholders, looping. Off by default.
- A background check runs in the web workers, so held failures and the above don't wait for an event.

**Fixed**

- A stream only drops once another on the channel has delivered data, so a channel that's off the air isn't shuffled.
- A flag on a stalled stream tries another account first, and a refusal in that time isn't blamed on the stream refused.
- A stream that recovered and is playing when its failure is judged isn't demoted.
- The zap guard goes by when the attempt began, covers a channel stopping just after it, and applies with no limit set in Dispatcharr.
- Second chances, earning back and outage rollback obey Dry run, Channels to leave alone and hand order.
- Undo leaves alone a stream that has already come back.
- A flag moves on to the next stream instead of back to the one that just failed, and does nothing with no viewer there.
- A failure after a long good play counts half.
- The stop warning now reaches Dispatcharr's notifications.
- A setting that can't be read keeps Dry run on.
- A deadlock with Dispatcharr's channel editor. Locks are now taken in id order.
- A failed switch after a flag reports `demoted`, not an error, and Status says when things are failing.
- Removing the plugin and putting it back clears old tokens and demotions.
- The flag route follows the Stream Endpoints network list.
- The button says how long a limit lasts. The nginx example checks the certificate.
- A journal label can't run as a spreadsheet formula.

## 0.1.0

First version.
