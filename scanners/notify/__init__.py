"""Scanner operational notifications.

One rule governs everything in this package: a notification is an
observation ABOUT a run, never a step IN one. Nothing here may change a
scanner's result, its stored signals, or the process exit code, and
every entry point is written so that a Slack outage is indistinguishable
-- from the run's point of view -- from a Slack success.

The package deliberately contains no candidate data. Symbols and scores
live in the analytics store and in the reports; an alert says that a run
broke, not what it found.
"""
