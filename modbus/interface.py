from abc import ABC, abstractmethod


class ModbusInterface(ABC):
    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def disconnect(self): ...

    @abstractmethod
    def read_holding_register(self, address: int) -> int: ...

    @abstractmethod
    def write_holding_register(self, address: int, value: int) -> bool: ...

    @abstractmethod
    def read_coil(self, address: int) -> bool: ...

    @abstractmethod
    def write_coil(self, address: int, value: bool) -> bool: ...

    @abstractmethod
    def read_discrete_input(self, address: int) -> bool: ...
