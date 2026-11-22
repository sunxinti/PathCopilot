# -*- coding: utf-8 -*-
"""In-process WSI registry for the MultiPathQA CLI evaluator.

The module is named ``app`` because ``run_multipathqa_eval.py`` and
``run_agent()`` register each slide as ``uploaded_files[file_id]``.
"""

uploaded_files = {}
