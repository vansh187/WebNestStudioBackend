"""The database host allowlist (hosts.json): data, not code. Validated once at
startup; a bad file stops the app from starting."""

import json
import logging
from pathlib import Path

from codelab_db.constants import ENGINES, LABEL_PATTERN, LOGGER_NAME
from codelab_db.errors import DbError, RegistryInvalid
from codelab_db.protocols import HostRule

logger = logging.getLogger(LOGGER_NAME)


def _pattern_labels(pattern: str) -> list[str]:
    try:
        labels = pattern.split(".")
        if len(labels) < 3:
            raise RegistryInvalid(f"pattern {pattern!r} needs at least three labels")
        for label in labels:
            if label != "*" and not LABEL_PATTERN.fullmatch(label):
                raise RegistryInvalid(f"pattern {pattern!r} has an invalid label {label!r}")
        fixed = [label for label in labels if label != "*"]
        if len(fixed) < 2 or labels[-1] == "*" or labels[-2] == "*":
            raise RegistryInvalid(f"pattern {pattern!r} must end in a fixed registrable domain")
        return labels
    except DbError:
        raise
    except Exception as exc:
        raise RegistryInvalid(f"pattern {pattern!r} could not be read") from exc


def _domain(labels: list[str]) -> str:
    """The fixed suffix of a pattern, e.g. pooler.supabase.com - the only
    host detail the proxy logs."""
    try:
        suffix: list[str] = []
        for label in reversed(labels):
            if label == "*":
                break
            suffix.insert(0, label)
        return ".".join(suffix)
    except Exception as exc:
        raise RegistryInvalid("a pattern's domain could not be read") from exc


class JsonHostAllowlist:
    def __init__(self, engines: dict[str, dict]) -> None:
        self._engines = engines

    def match(self, engine: str, host: str) -> HostRule | None:
        """Exact label-for-label match: "*" is exactly one label, so
        a.b.pooler.supabase.com does not match *.pooler.supabase.com."""
        try:
            labels = host.split(".")
            for rule, rule_labels in self._engines.get(engine, {}).get("rules", ()):
                if len(rule_labels) == len(labels) and all(
                    expected == "*" and LABEL_PATTERN.fullmatch(actual) or expected == actual
                    for expected, actual in zip(rule_labels, labels)
                ):
                    return rule
            return None
        except Exception as exc:
            logger.exception("host allowlist lookup failed engine=%s", engine)
            raise RegistryInvalid("host lookup failed") from exc

    def ports(self, engine: str) -> tuple[int, ...]:
        try:
            return self._engines[engine]["ports"]
        except KeyError as exc:
            raise RegistryInvalid(f"no ports for {engine}") from exc

    def default_port(self, engine: str) -> int:
        try:
            return self._engines[engine]["default_port"]
        except KeyError as exc:
            raise RegistryInvalid(f"no default port for {engine}") from exc

    def describe(self) -> dict:
        try:
            return {
                engine: {
                    "ports": list(config["ports"]),
                    "hosts": [{"pattern": rule.pattern, "provider": rule.label, "tls_verified": rule.tls_verify}
                              for rule, _ in config["rules"]],
                }
                for engine, config in self._engines.items()
            }
        except Exception as exc:
            logger.exception("host allowlist description failed")
            raise RegistryInvalid("the allowlist could not be described") from exc


def parse_allowlist(data: object) -> JsonHostAllowlist:
    try:
        if not isinstance(data, dict) or set(data) != set(ENGINES):
            raise RegistryInvalid(f"the allowlist must have exactly these engines: {', '.join(ENGINES)}")
        engines: dict[str, dict] = {}
        seen_patterns: set[str] = set()
        for engine in ENGINES:
            config = data[engine]
            if not isinstance(config, dict):
                raise RegistryInvalid(f"{engine} must be an object")
            ports = config.get("ports")
            if (not isinstance(ports, list) or not ports
                    or not all(isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535
                               for port in ports)):
                raise RegistryInvalid(f"{engine}.ports must be a non-empty list of port numbers")
            default_port = config.get("default_port")
            if default_port not in ports:
                raise RegistryInvalid(f"{engine}.default_port must be one of its ports")
            hosts = config.get("hosts")
            if not isinstance(hosts, list) or not hosts:
                raise RegistryInvalid(f"{engine}.hosts must be a non-empty list")
            rules = []
            for entry in hosts:
                if not isinstance(entry, dict):
                    raise RegistryInvalid(f"{engine}.hosts entries must be objects")
                pattern, verify, label = entry.get("pattern"), entry.get("tls_verify"), entry.get("label")
                if not isinstance(pattern, str) or pattern != pattern.lower():
                    raise RegistryInvalid(f"{engine}: patterns must be lowercase strings")
                if not isinstance(verify, bool):
                    raise RegistryInvalid(f"{engine}: {pattern} needs tls_verify true or false")
                if not isinstance(label, str) or not label.strip():
                    raise RegistryInvalid(f"{engine}: {pattern} needs a label")
                if (engine, pattern) in seen_patterns:
                    raise RegistryInvalid(f"{engine}: {pattern} appears twice")
                seen_patterns.add((engine, pattern))
                labels = _pattern_labels(pattern)
                rules.append((HostRule(pattern, verify, label, _domain(labels)), labels))
            engines[engine] = {"ports": tuple(ports), "default_port": default_port, "rules": tuple(rules)}
        return JsonHostAllowlist(engines)
    except DbError:
        raise
    except Exception as exc:
        raise RegistryInvalid("the allowlist could not be read") from exc


def load_host_allowlist(path: str) -> JsonHostAllowlist:
    try:
        allowlist = parse_allowlist(json.loads(Path(path).read_text(encoding="utf-8")))
        logger.info("CodeLab database proxy hosts loaded from %s", Path(path).name)
        return allowlist
    except DbError:
        logger.critical("CodeLab database host allowlist %s is invalid", path, exc_info=True)
        raise
    except (OSError, ValueError) as exc:
        logger.critical("could not load the CodeLab database host allowlist %s", path, exc_info=True)
        raise RegistryInvalid(f"could not load {path}") from exc
