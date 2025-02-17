from abc import ABC, abstractmethod


class DataSourceClient(ABC):

    def __init__(self):
        pass

    def connect(self):
        pass

    @property
    @abstractmethod
    def description(self):
        pass

    @abstractmethod
    def test_connection(self):
        pass

    @abstractmethod
    def get_schemas(self):
        pass

    @abstractmethod
    def get_schema(self, table_name):
        pass

    @abstractmethod
    def prompt_schema(self):
        pass

    @abstractmethod
    def execute_query(self, **kwargs):
        pass
