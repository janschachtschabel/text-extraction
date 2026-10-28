"""PII handling: finding personal data in extraction results and replacing or reporting it.

This package root stays empty on purpose. The API image imports the models and the pure helpers
(``types``, ``spans``, ``replace``, ``fields``, ``html``); only the NLP worker's child processes
import Presidio and spaCy (``child``, ``recognizers``), and nothing else here may pull them in.
"""
