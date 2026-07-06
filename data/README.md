# Data Folder

This folder is for non-private schemas, manifests, and derived labels.

Raw media and identifiable outputs should stay out of git. The `.gitignore` excludes `data/raw/`, thumbnails, videos, logs, and common media files by default.

Recommended workflow:

1. Put raw local clips outside git or under `data/raw/`.
2. Store pose-only and sensor-only derived records whenever possible.
3. Label clips, events, sensor events, routine transitions, and alert reviews with the CSV files in `data/labels/`.
4. Review false alerts and missed concerns before changing thresholds.
