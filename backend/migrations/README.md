# 数据表初始化

本版通过 `src.models.base.Base.metadata.create_all` 创建首次运行所需的十张表。模型在 `src/models/workflow.py`。`python -m scripts.seed_demo` 按观测内容键补全演示样本，保留现有分析及签核。数据库端口只发布到本机。

新增的四张补差表：

- `bills`：原热水账单，冻结建账时的费率、原始区间和原始透视结果，之后不再改写。
- `late_readings`：晚到表底，记录表计、时刻、累计表底和归属补差批次，可据此追到对应表底。
- `adjustments`：补差批次，待结（pending）随新读数实时重算，提交（submitted）冻结金额，结清（settled）后不可重复入账。
- `settlement_attempts`：结账尝试与幂等键，失败重试不会重复入账。

