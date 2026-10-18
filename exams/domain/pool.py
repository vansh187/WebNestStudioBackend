"""Fixed pool targets per module and readiness (plan section 6.2)."""

import uuid


def module_targets(lesson_counts: dict[uuid.UUID, int], pool_target: int, min_per_module: int) -> dict[uuid.UUID, int]:
    """Splits the course pool across modules by lesson count (largest
    remainder), at least `min_per_module` each. When modules x minimum reaches
    the target, every module simply gets the minimum."""
    modules = sorted((m for m, count in lesson_counts.items() if count > 0), key=str)
    if not modules:
        return {}
    if len(modules) * min_per_module >= pool_target:
        return {m: min_per_module for m in modules}

    total_lessons = sum(lesson_counts[m] for m in modules)
    shares = {m: pool_target * lesson_counts[m] / total_lessons for m in modules}
    targets = {m: max(min_per_module, int(shares[m])) for m in modules}
    diff = pool_target - sum(targets.values())
    while diff > 0:
        for m in sorted(modules, key=lambda m: (-(shares[m] - targets[m]), str(m))):
            if diff == 0:
                break
            targets[m] += 1
            diff -= 1
    while diff < 0:
        # Raising small modules to the minimum overshot: take from the modules
        # furthest above their share, never below the minimum.
        m = max((m for m in modules if targets[m] > min_per_module), key=lambda m: (targets[m] - shares[m], str(m)))
        targets[m] -= 1
        diff += 1
    return targets


def pool_is_full(targets: dict[uuid.UUID, int], live_counts: dict[uuid.UUID, int]) -> bool:
    return bool(targets) and all(live_counts.get(m, 0) >= target for m, target in targets.items())
