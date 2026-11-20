.PHONY: install backend frontend test migrate-rehearse

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm ci

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

test:
	backend/.venv/bin/python -I -m unittest discover -s backend/tests -v

# 离线演练槽位迁移：迁移 -> 暂停 -> 恢复 -> 入库 -> 重试 -> 回滚
migrate-rehearse:
	backend/.venv/bin/python -I backend/scripts/slot_migration.py rehearse --out /tmp/slot-rehearsal.json
