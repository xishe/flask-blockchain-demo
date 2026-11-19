import sqlite3
from datetime import datetime, timezone

from flask import Flask, redirect, render_template, request, url_for
from werkzeug.exceptions import RequestEntityTooLarge

import blockchain


def create_app(test_config=None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DATABASE_PATH=blockchain.get_default_db_path(),
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    if test_config is not None:
        app.config.update(test_config)

    @app.template_filter("block_time")
    def block_time(timestamp):
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")
        except (OSError, OverflowError, ValueError, TypeError):
            return "Время не читается"

    def page(*, status=200, chain=None, **context):
        if chain is None:
            chain = blockchain.get_chain(app.config["DATABASE_PATH"])
        return render_template(
            "index.html", chain=chain, max_text_length=blockchain.MAX_TEXT_LENGTH, **context
        ), status

    @app.errorhandler(sqlite3.Error)
    @app.errorhandler(OSError)
    def storage_unavailable(error):
        app.logger.warning("Unable to access blockchain storage (%s)", type(error).__name__)
        return page(chain=[], storage_error="Хранилище блоков недоступно.", status=503)

    @app.errorhandler(blockchain.LegacyChainError)
    def bad_legacy_data(error):
        return page(chain=[], storage_error=f"Импорт старой цепочки остановлен. {error}", status=409)

    @app.errorhandler(blockchain.CorruptChainError)
    @app.errorhandler(blockchain.MiningLimitError)
    def invalid_chain(error):
        return page(error=str(error), status=409)

    @app.errorhandler(RequestEntityTooLarge)
    def oversized_request(error):
        return page(chain=[], error="Запрос слишком большой.", status=413)

    @app.route("/", methods=["GET", "POST"])
    def index():
        if request.method == "GET":
            return page()

        text = request.form.get("text", "")
        try:
            blockchain.write_block(
                text,
                make_proof=request.form.get("make_proof") in {"on", "1", "true"},
                db_path=app.config["DATABASE_PATH"],
            )
        except blockchain.LegacyChainError:
            raise
        except ValueError as error:
            return page(error=str(error), draft_text=text[:blockchain.MAX_TEXT_LENGTH], status=400)
        return redirect(url_for("index"), code=303)

    @app.post("/check")
    def integrity():
        chain = blockchain.get_chain(app.config["DATABASE_PATH"])
        return page(chain=chain, results=blockchain.validate_chain(chain))

    @app.post("/mining")
    def mining():
        count = blockchain.mine_pending_blocks(app.config["DATABASE_PATH"])
        message = f"Вычислено доказательств: {count}." if count else "Ожидающих блоков: 0."
        return page(mining_message=message)

    return app


app = create_app()


if __name__ == "__main__":
    app.run()
