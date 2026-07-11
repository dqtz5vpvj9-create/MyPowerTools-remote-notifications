from logging_lib import LogLevel, setup_logging
l = setup_logging(console_level=LogLevel.DEBUG)
l.debug("Hello world")
