---
description: Set Cameron's availability on the Agent Bus (studying, lecture, gym, meal, sleeping, focus, away, available) so every agent adapts and low-priority phone pings are held
argument-hint: <studying|lecture|gym|meal|sleeping|focus|away|available> [note]
allowed-tools: Bash(C:/Users/camer/AppData/Local/Programs/Python/Python312/python.exe C:/Users/camer/Desktop/Projects/notifications/bus.py:*)
---

Cameron ran `/agent-bus:mode $ARGUMENTS`. The Agent Bus command has already run; its output:

!`C:/Users/camer/AppData/Local/Programs/Python/Python312/python.exe C:/Users/camer/Desktop/Projects/notifications/bus.py --as user state "$ARGUMENTS"`

Answer Cameron in one or two short lines: the mode now in effect, until when, and which phone priorities still reach his phone. If no mode was given, the output is the current state; list the modes he can pass (studying, lecture, gym, meal, sleeping, focus, away, available, or any other word plus a note). If the command failed, show its one-line error. Do not run anything else.
