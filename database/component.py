"""Shared infrastructure for synchronous database components."""


class DatabaseComponent:
    def __init__(self, engine, session_factory):
        self.engine = engine
        self.Session = session_factory

    def get_session(self):
        return self.Session()
