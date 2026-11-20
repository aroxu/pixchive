# Browser helper third-party material

`vendor/seccomp_profile.json` is the Docker seccomp profile distributed with
Microsoft Playwright v1.64.1, retained without modification. Source:
https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json

Playwright is Copyright (c) Microsoft Corporation, licensed under Apache License
2.0. Its license is included in `vendor/playwright-LICENSE`. The profile builds
on Docker's default seccomp policy, also distributed under Apache License 2.0.

The official Playwright Python image contains Chromium and other upstream
components with their own notices. Keep the upstream image's notices and the
installed Python distributions' license metadata when redistributing this image.
