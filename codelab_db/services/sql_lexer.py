"""Splits SQL into code, strings, quoted identifiers and comments the way the
database server does, so the statement guard looks at what will actually run.

Postgres: -- and nested /* */ comments, '...' with '' escapes, E'...' with
backslash escapes, "identifiers", $tag$...$tag$ bodies.
MySQL: #, "-- " and /* */ comments, /*! ... */ executable comments (their
content IS code), '...' and "..." strings with backslash escapes,
`identifiers`.

Anything unterminated is refused rather than guessed at."""

import logging
import re
from dataclasses import dataclass

from codelab_db.constants import LOGGER_NAME
from codelab_db.errors import BadRequest, DbError

logger = logging.getLogger(LOGGER_NAME)

STRING = "'?'"        # placeholder a string leaves in the code view
IDENT = '"?"'         # placeholder a quoted identifier leaves in the code view
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_IDENT_CHAR = re.compile(r"[A-Za-z0-9_$]")
_MYSQL_EXEC_COMMENT = re.compile(r"/\*M?!\d{0,6}")


@dataclass(frozen=True)
class LexedSql:
    code: str          # comments removed; strings and quoted identifiers replaced by placeholders
    code_bodies: str   # like code, plus the inside of $...$ bodies, strings included (they can be EXECUTEd)
    text: str          # comments removed; string and identifier contents kept, quotes dropped
    statements: int    # non-empty statements separated by ; in the code
    semicolons: tuple[int, ...] = ()   # positions in the SQL of the code-level ; separators


class _Lexer:
    def __init__(self, sql: str, dialect: str) -> None:
        self.sql = sql
        self.dialect = dialect
        self.position = 0
        self.code: list[str] = []
        self.bodies: list[str] = []
        self.text: list[str] = []
        self.semicolons: list[int] = []
        self.exec_comments = 0

    def run(self) -> LexedSql:
        try:
            while self.position < len(self.sql):
                self._step()
            if self.exec_comments:
                raise BadRequest("the SQL has an unterminated comment")
            code = "".join(self.code)
            statements = sum(1 for part in code.split(";") if part.strip())
            return LexedSql(code, "".join(self.bodies), "".join(self.text), statements, tuple(self.semicolons))
        except DbError:
            raise
        except Exception as exc:
            logger.warning("SQL lexer failed type=%s", type(exc).__name__)
            raise BadRequest("the SQL could not be read") from exc

    def _emit(self, code: str, body: str | None = None, text: str | None = None) -> None:
        try:
            self.code.append(code)
            self.bodies.append(code if body is None else body)
            self.text.append(code if text is None else text)
        except Exception as exc:
            raise BadRequest("the SQL could not be read") from exc

    def _step(self) -> None:
        try:
            sql, i = self.sql, self.position
            char, pair = sql[i], sql[i:i + 2]
            if self.dialect == "mysql":
                if char == "#":
                    return self._skip_line()
                if pair == "--" and (i + 2 == len(sql) or sql[i + 2] in " \t\r\n\f\v"):
                    return self._skip_line()
                exec_comment = _MYSQL_EXEC_COMMENT.match(sql, i)
                if exec_comment:
                    self.exec_comments += 1
                    self.position = exec_comment.end()
                    return self._emit(" ")
                if pair == "*/" and self.exec_comments:
                    self.exec_comments -= 1
                    self.position += 2
                    return self._emit(" ")
                if pair == "/*":
                    return self._skip_block(nested=False)
                if char in "'\"":
                    return self._string(char, backslash=True)
                if char == "`":
                    return self._identifier("`")
            else:
                if pair == "--":
                    return self._skip_line()
                if pair == "/*":
                    return self._skip_block(nested=True)
                if char == "'":
                    escape = i > 0 and sql[i - 1] in "eE" and (i < 2 or not _IDENT_CHAR.match(sql[i - 2]))
                    return self._string("'", backslash=escape)
                if char == '"':
                    return self._identifier('"')
                if char == "$" and (i == 0 or not _IDENT_CHAR.match(sql[i - 1])):
                    tag = _DOLLAR_TAG.match(sql, i)
                    if tag:
                        return self._dollar(tag.group(0))
            if char == ";":
                self.semicolons.append(i)
            self.position += 1
            self._emit(char)
        except DbError:
            raise
        except Exception as exc:
            raise BadRequest("the SQL could not be read") from exc

    def _skip_line(self) -> None:
        try:
            end = self.sql.find("\n", self.position)
            self.position = len(self.sql) if end < 0 else end + 1
            self._emit(" ")
        except Exception as exc:
            raise BadRequest("the SQL could not be read") from exc

    def _skip_block(self, nested: bool) -> None:
        try:
            depth, i = 0, self.position
            while i < len(self.sql):
                pair = self.sql[i:i + 2]
                if pair == "/*" and (nested or depth == 0):
                    depth, i = depth + 1, i + 2
                elif pair == "*/":
                    depth, i = depth - 1, i + 2
                    if depth == 0:
                        self.position = i
                        return self._emit(" ")
                else:
                    i += 1
            raise BadRequest("the SQL has an unterminated comment")
        except DbError:
            raise
        except Exception as exc:
            raise BadRequest("the SQL could not be read") from exc

    def _string(self, quote: str, backslash: bool) -> None:
        try:
            i, content = self.position + 1, []
            while i < len(self.sql):
                char = self.sql[i]
                if backslash and char == "\\" and i + 1 < len(self.sql):
                    content.append(self.sql[i + 1])
                    i += 2
                elif char == quote:
                    if self.sql[i + 1:i + 2] == quote:
                        content.append(quote)
                        i += 2
                    else:
                        self.position = i + 1
                        return self._emit(STRING, STRING, " " + "".join(content) + " ")
                else:
                    content.append(char)
                    i += 1
            raise BadRequest("the SQL has an unterminated string")
        except DbError:
            raise
        except Exception as exc:
            raise BadRequest("the SQL could not be read") from exc

    def _identifier(self, quote: str) -> None:
        try:
            i, content = self.position + 1, []
            while i < len(self.sql):
                char = self.sql[i]
                if char == quote:
                    if self.sql[i + 1:i + 2] == quote:
                        content.append(quote)
                        i += 2
                    else:
                        self.position = i + 1
                        # The text view keeps the bare name, so "pg_read_file"(...) is still seen as a call.
                        return self._emit(IDENT, IDENT, "".join(content))
                else:
                    content.append(char)
                    i += 1
            raise BadRequest("the SQL has an unterminated quoted name")
        except DbError:
            raise
        except Exception as exc:
            raise BadRequest("the SQL could not be read") from exc

    def _dollar(self, tag: str) -> None:
        try:
            start = self.position + len(tag)
            end = self.sql.find(tag, start)
            if end < 0:
                raise BadRequest("the SQL has an unterminated $-quoted body")
            body = self.sql[start:end]
            self.position = end + len(tag)
            # Code view: an opaque string. Body view: the body with its string
            # contents, since function and DO bodies run later and can run SQL
            # held in strings (EXECUTE '...'). A body that isn't valid SQL on
            # its own (plain text such as $$it's$$) is scanned as-is.
            try:
                nested = _Lexer(body, self.dialect).run()
                body_code, body_text = nested.text, nested.text
            except BadRequest:
                body_code = body_text = body
            self._emit(STRING, " " + body_code + " ", " " + body_text + " ")
        except DbError:
            raise
        except Exception as exc:
            raise BadRequest("the SQL could not be read") from exc


def lex(sql: str, dialect: str) -> LexedSql:
    try:
        return _Lexer(sql, dialect).run()
    except DbError:
        raise
    except RecursionError as exc:
        raise BadRequest("the SQL nests $-quoted bodies too deeply") from exc
    except Exception as exc:
        logger.warning("SQL lexing failed type=%s", type(exc).__name__)
        raise BadRequest("the SQL could not be read") from exc
