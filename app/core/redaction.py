"""Defense in depth: redact credential-shaped values from every log record."""

import logging
import re

PATTERNS = [
    (re.compile(r"(PHPSESSID=)[^;\s\"',]+", re.IGNORECASE), r"\1[redacted]"),
    (
        re.compile(r"((?:cookie|phpsessid)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+", re.IGNORECASE),
        r"\1[redacted]",
    ),
    # PHPSESSID values look like {user id}_{random}; Fernet tokens start with gAAAAA.
    (re.compile(r"\b[0-9]{1,20}_[A-Za-z0-9]{20,}\b"), "[redacted]"),
    (re.compile(r"gAAAAA[A-Za-z0-9_\-=]{20,}"), "[redacted]"),
    (re.compile(r"(pixchive_session=)[^;\s]+"), r"\1[redacted]"),
]


def redact(value: str) -> str:
    for pattern, replacement in PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def clean(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(value if isinstance(value, str) else str(value))


class RedactingFilter(logging.Filter):
    """Keeps the argument shape so formatters such as uvicorn's access log still work."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        else:
            record.msg = clean(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(clean(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: clean(value) for key, value in record.args.items()}
        if record.exc_info:
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_text, record.exc_info = redact(text), None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install():
    """Attach to every configured handler, including uvicorn and arq handlers."""
    shared = RedactingFilter()
    loggers = [logging.getLogger()] + [
        item
        for item in logging.Logger.manager.loggerDict.values()
        if isinstance(item, logging.Logger)
    ]
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(shared)
