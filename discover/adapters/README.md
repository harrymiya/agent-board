# Agent adapters

每个适配器负责一种 Agent 的三件事：

1. 识别自己的进程
2. 读取该 Agent 的本地会话/日志数据
3. 通过 `DiscoveryContext` 写出看板卡片

核心入口不会依赖某个 Agent 的数据库结构。内置适配器放在本目录中；第三方适配器可以放在自己的 Python 包里，通过环境变量加载：

```bash
AGENTBOARD_ADAPTERS=my_agentboard_adapter python3 discover/hermes_board_discover.py
```

最小适配器示例：

```python
from discover.adapters.base import AgentAdapter


class MyAdapter(AgentAdapter):
    name = "my-agent"

    def matches(self, comm, cmdline):
        return comm == "my-agent"

    def discover(self, context, processes):
        total = 0
        for process in processes:
            if process.kind == self.name:
                context.write_process(process, self.name)
                total += 1
        return total


adapter = MyAdapter()
```

适配器应尽量只读取本地数据，不修改 Agent 自己的文件；不可用的数据源应返回空结果，让其他适配器继续工作。
