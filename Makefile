PY ?= .venv/bin/python
WORK ?= work

.PHONY: setup test demo demo-auto mutation clean

setup:            ## 建 venv 并安装依赖
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt

test:             ## 5 个测试(不需要 LLM key)
	$(PY) -m pytest -q

demo:             ## 完整走一遍:造数据 → 停在人工确认 → 查看等待项 → 回答 → 出月表/月报
	rm -rf $(WORK)
	$(PY) -m demo.run --thread june --workdir $(WORK) --no-ai > /dev/null
	$(PY) -m demo.run --thread june --workdir $(WORK) --status
	$(PY) -m demo.run --thread june --workdir $(WORK) --decide '{"51":"确认异常","54":"核销"}'
	@echo "产物:" && ls $(WORK)/output

demo-auto:        ## 一口气跑完,不停在人工确认(未决项写进备注)
	rm -rf $(WORK)
	$(PY) -m demo.run --thread june --workdir $(WORK) --no-ai --auto

mutation:         ## 故障注入检查:10 处变异逐一跑测试
	$(PY) eval/mutation_check.py

clean:
	rm -rf $(WORK) .pytest_cache
