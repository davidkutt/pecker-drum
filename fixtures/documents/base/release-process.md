---
title: Release process
domains: process, platform
visibility: internal
---
We release every Tuesday and Thursday. The release manager of the month cuts the release branch at
10:00, runs the release checklist and announces the release in the team channel.

A release needs a green CI run on the release branch and sign-off from one reviewer outside the
author's squad. Hotfixes follow the same steps but may skip the release window with approval from
the on-call lead.

If a release breaks production, roll back first and investigate afterwards.
