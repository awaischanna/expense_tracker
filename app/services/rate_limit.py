"""Failed-login rate limit: 5 failures per email per 15 minutes, stored in Redis.

Why Redis + Lua: the counter lives in one shared place, so it is correct when the API
runs on many servers, and each script runs atomically inside Redis.

How it works ("reserve, then release on success"):
  1. Before checking the password we atomically INCR the counter (reserving a slot).
     If that pushes it over the limit we undo the INCR and return 429.
  2. If the password is wrong, the slot stays used (it *is* a failed login).
  3. If the password is right, we give the slot back (DECR).
Because the check and the increment are one atomic step, 10 parallel wrong logins on
3 servers can never get more than 5 password checks through. A naive
"GET count, check password, INCR" would let parallel requests slip past the limit.
The window starts at the first failure (fixed window) - simple and good enough here.
"""

import hashlib

from app.config import get_settings
from app.errors import TooManyRequests
from app.redis_client import get_redis

_RESERVE = """
local count = redis.call('INCR', KEYS[1])
if count == 1 or redis.call('TTL', KEYS[1]) == -1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
if count > tonumber(ARGV[2]) then
  redis.call('DECR', KEYS[1])
  return {0, redis.call('TTL', KEYS[1])}
end
return {1, count}
"""

_RELEASE = """
if redis.call('EXISTS', KEYS[1]) == 1 and tonumber(redis.call('GET', KEYS[1])) > 0 then
  redis.call('DECR', KEYS[1])
end
return 1
"""


def _key(email: str) -> str:
    # Hash so we don't keep raw emails as Redis keys.
    return "login_fail:" + hashlib.sha256(email.lower().encode()).hexdigest()


async def reserve_login_attempt(email: str) -> None:
    s = get_settings()
    allowed, value = await get_redis().eval(  # type: ignore[misc]
        _RESERVE, 1, _key(email), s.login_window_seconds, s.login_max_failures
    )
    if not int(allowed):
        retry_after = max(int(value), 1)
        raise TooManyRequests(
            "Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
            details=[{"retry_after_seconds": retry_after}],
        )


async def release_login_attempt(email: str) -> None:
    await get_redis().eval(_RELEASE, 1, _key(email))  # type: ignore[misc]
