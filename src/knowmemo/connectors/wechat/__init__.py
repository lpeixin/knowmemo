"""WeChat connector package.

Importing this package registers :class:`WeChatConnector` with the global
:data:`~knowmemo.ingestion.interfaces.registry`, which is what makes
``knowmemo source scan wechat`` work. Registration is a side effect of import
by design — the registry is populated in one obvious place rather than by a
plugin discovery mechanism whose behaviour depends on the working directory.
"""

from knowmemo.connectors.wechat.extractor import SOURCE_TYPE, WeChatConnector
from knowmemo.ingestion.interfaces import registry

registry.register(WeChatConnector)

__all__ = ["SOURCE_TYPE", "WeChatConnector"]
