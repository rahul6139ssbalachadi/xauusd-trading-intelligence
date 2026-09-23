# Self-Improvement Report — What Was Fixed

## Phase 1: Backend (FastAPI)

### Security Hardening
1. **Environment variables**: `TRADING_SECRET_KEY`, `TRADING_DB_PATH`, `TRADING_TOKEN_EXPIRE_MINUTES`, `TRADING_ALLOWED_ORIGINS` — no more hardcoded secrets
2. **Rate limiting**: 5 requests/minute per IP on login — prevents brute force
3. **Input validation**: Password min 8 chars, role enum check, email format via `pydantic.EmailStr`
4. **Pydantic V2**: Replaced deprecated `@validator` with `@field_validator`

### Bug Fixes
5. **Database connection leak**: Replaced async generator `get_db()` with `@contextmanager` — the old pattern closed the connection before queries ran in async endpoints (`sqlite3.ProgrammingError: Cannot operate on a closed database`)
6. **Path resolution**: DB path now uses `get_db_path()` function (reads env var each time) instead of module-level constant — enables test patching

### New Tests
7. **14 API tests**: login (success/wrong password/nonexistent), token validation (valid/missing/invalid), RBAC (admin-only register, client cannot access admin), health check, trades/scoping, equity, summary

---

## Phase 2-4: Flutter

### Bug Fixes
8. **`context.read` → `ref.read`**: Fixed in `ClientDetailScreen.build()` where `context.read(apiProvider)` was used inside a `StatelessWidget` that doesn't have a `ref`
9. **StatusBadge int comparison**: Risk state `kill_switch_active` is stored as BOOLEAN (0/1) in SQLite — comparison `== 1` works but should use `!= 0` for cross-DB compatibility

---

## Database
10. **Performance indexes**: Added on `trades(client_id, status, entry_time)`, `equity_curve(client_id, date)`, `risk_state(client_id)`, `users(email, client_id)`

---

## Files Changed/Created

| File | Action |
|------|--------|
| `api/main.py` | Hardened + fixed DB handling |
| `tests/test_api.py` | 14 new tests |
| `mobile/lib/admin/admin_home_screen.dart` | Fixed `context.read` bug |
| `scripts/add_indexes.py` | New index migration script |
| `.gitignore` | Already clean |

---

## Test Results

- **Trading engine tests**: 206 passed, 0 regressions
- **API tests**: 14 passed, 0 failures
- **Total**: 220 tests passing

---

## Known Gaps (intentional — not yet in scope)

1. **Token refresh** — no refresh token endpoint yet (client uses long-lived access tokens)
2. **WebSocket/push notifications** — FCM integration pending
3. **Biometric auth UI** — placeholder only (needs platform-specific config)
4. **macOS/Windows desktop builds** — need Xcode + Visual Studio installed locally
5. **Admin write/control actions** — explicitly deferred per your instruction
6. **Flutter pub get** — dependencies not yet installed (no `flutter` SDK in this env)
