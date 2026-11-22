# -*- coding: utf-8 -*-
"""In-process WSI path registry for the headless cohort runner.

``run_fulleval.py`` and ``run_agent()`` register each slide as
``uploaded_files[file_id]`` before analysis. Keep this module import-safe:
no web framework, no API keys, no disk side effects.
"""

uploaded_files = {}
