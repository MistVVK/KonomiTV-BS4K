from collections.abc import MutableMapping

from app.constants import LOGGING_CONFIG


# テスト収集時に app.logging が初期化されても、実稼働用ログファイルへ触れないようにする。
# Docker で作成されたログがホストユーザーから書き込めない場合にもテストを実行できる。
_FILE_HANDLERS = {'default_file', 'debug_file', 'access_file'}
handlers = LOGGING_CONFIG.get('handlers')
if isinstance(handlers, MutableMapping):
    for handler_name in _FILE_HANDLERS:
        handlers.pop(handler_name, None)

loggers = LOGGING_CONFIG.get('loggers')
if isinstance(loggers, MutableMapping):
    for logger_config in loggers.values():
        if not isinstance(logger_config, MutableMapping):
            continue
        logger_handlers = logger_config.get('handlers')
        if isinstance(logger_handlers, list):
            logger_config['handlers'] = [
                handler_name
                for handler_name in logger_handlers
                if handler_name not in _FILE_HANDLERS
            ]
