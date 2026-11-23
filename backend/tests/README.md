# 测试

三套测试分别覆盖补差业务的不同层级，均不依赖 MySQL 或 Redis。

| 文件 | 层级 | 依赖 |
| --- | --- | --- |
| `test_billing_domain.py` | 纯领域算法（拆区间、原费率、分时补差、勾稽、尾差） | 仅标准库 |
| `test_billing_integration.py` | SQLAlchemy 服务编排（待结/冻结/结清、幂等、追溯） | requirements 依赖，SQLite 内存库 |
| `test_billing_api.py` | FastAPI 路由全链路（JWT 角色、真实请求/响应、结账重试） | requirements 依赖，SQLite 文件库 + 假 Redis |

在容器或已安装 `requirements.txt` 的环境中，从 `backend` 目录运行：

```sh
python -m unittest discover -s tests -v
```

领域测试可在仅有标准库的环境直接运行：

```sh
python3 -m tests.test_billing_domain
```

SQLite 测试通过 `DATABASE_URL=sqlite+pysqlite:///...` 覆盖数据库地址；`test_billing_api.py` 会在 `setUpClass` 中把各路由模块引用的 `Session` 重新绑定到临时文件库，并用内存假对象替代 Redis 登录限流和唤醒通道。
