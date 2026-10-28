"""PII handling: finding personal data in extraction results and replacing or reporting it.

This package root stays empty on purpose. The API image imports the models and the pure helpers
(``types``, ``spans``, ``replace``); only the NLP worker's child processes import Presidio and
spaCy, and nothing here may pull them in.
"""
