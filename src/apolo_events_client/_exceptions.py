from typing import Any, cast
from uuid import UUID


class ServerError(Exception):
    def __init__(
        self,
        code: str,
        descr: str,
        details_head: str | None,
        details: Any,
        msg_id: UUID | None,
    ) -> None:
        super().__init__(code, descr, details_head, details, msg_id)

    @property
    def code(self) -> str:
        return cast(str, self.args[0])

    @property
    def descr(self) -> str:
        return cast(str, self.args[1])

    @property
    def details_head(self) -> str | None:
        return cast(str | None, self.args[2])

    @property
    def details(self) -> Any:
        return self.args[3]

    @property
    def msg_id(self) -> UUID | None:
        return cast(UUID | None, self.args[4])
