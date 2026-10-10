# Third-party notices

Pixchive's original code is licensed under [MIT](LICENSE). Third-party packages,
fonts, and tools retain their own licenses; the project license does not replace
them.

The frontend uses Noto Sans KR under SIL Open Font License 1.1 and dependencies
including React, HeroUI, React Router, and TanStack Query. `npm run licenses`
collects the installed runtime packages' license texts and attribution into
`frontend/dist/assets/third-party-licenses.txt` after the frontend build. The
Docker build includes this file and serves it at `/assets/third-party-licenses.txt`.

Python packages retain their installed distribution metadata and license files
in the container virtual environment. The Debian packages, including FFmpeg,
retain their notices under `/usr/share/doc`. Version locks are `uv.lock` and
`frontend/package-lock.json`.

The Pinchive project is acknowledged as a visual reference in [PLAN.md](PLAN.md).
Pixiv artwork, account metadata, and user archives are runtime data and are not
distributed as part of this repository or image.
