# Atajos para el dia a dia. Todo lo que hace CI se puede correr aqui igual.
.PHONY: ayuda venv pruebas build limpiar \
        publicar-dev publicar-qa publicar-prod \
        verificar-dev verificar-qa verificar-prod verificar-todo

PY := .venv/bin/python

ayuda:
	@echo "  make venv             crea el entorno virtual con las dependencias"
	@echo "  make pruebas          corre todas las suites"
	@echo "  make build            construye dist/ (artefactos reproducibles)"
	@echo ""
	@echo "  make publicar-dev     publica dist/ en dev"
	@echo "  make publicar-qa      publica dist/ en qa"
	@echo "  make publicar-prod    publica dist/ en prod"
	@echo ""
	@echo "  make verificar-dev    compara dev con dist/"
	@echo "  make verificar-qa     compara qa con dist/"
	@echo "  make verificar-prod   compara prod con dist/"
	@echo "  make verificar-todo   los tres de golpe"
	@echo ""
	@echo "  make limpiar          borra dist/, .build/ y el venv"

venv:
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip --quiet
	$(PY) -m pip install -r layer/requirements.txt boto3 --quiet
	@echo "listo: .venv"

pruebas:
	@PYTHON=$(PY) ./tests/run.sh

build:
	@./scripts/build.sh

publicar-dev:   ; @./scripts/publish.sh dev
publicar-qa:    ; @./scripts/publish.sh qa
publicar-prod:  ; @./scripts/publish.sh prod

verificar-dev:  ; @./scripts/verify.sh dev
verificar-qa:   ; @./scripts/verify.sh qa
verificar-prod: ; @./scripts/verify.sh prod

# Util despues de un terraform apply: si el ignore_changes se cayo, aqui se ve
# en que entorno se revirtio el codigo.
verificar-todo:
	@rc=0; for e in dev qa prod; do ./scripts/verify.sh $$e || rc=1; done; exit $$rc

limpiar:
	rm -rf dist .build .venv
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
