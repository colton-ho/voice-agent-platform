"""In-memory Service Bus 队列替身（B-3 契约测试）。

镜像 tests/fake_cosmos.py 的注入模式：set_queue_provider 挂载后，
storage.servicebus.get_queue 返回 FakeQueue。broker 语义最小化建模：
send 入待投队列、receive 取出（delivery_count 自增）、complete 摘除、
abandon 重新入队（超 max_delivery_count 次投递 → dlq，与 §2.1 对齐）。
"""
from storage.servicebus import QueueMessage


class FakeQueue:
    def __init__(self, name: str = "postcall-jobs", max_delivery_count: int = 5):
        self.name = name
        self.max_delivery_count = max_delivery_count
        self.sent: list = []       # (message_id, body) 发送记录
        self.completed: list = []  # 已结算消息
        self.abandoned: list = []  # 显式 abandon 记录
        self.dlq: list = []        # 投递耗尽的死信
        self._pending: list = []   # 待投递 (message_id, body, delivery_count)

    def send(self, *, message_id: str, body: dict) -> None:
        self.sent.append((message_id, dict(body)))
        # 首投报 1（「times delivered」约定；消费侧 is_final_delivery 按双约定安全侧收口）
        self._pending.append((message_id, dict(body), 1))

    def inject(self, body: dict, *, delivery_count: int, message_id: str | None = None):
        """测试直投：预设 delivery_count 的消息（I-4 边界腿用）。"""
        mid = message_id or body.get("job_id") or "injected"
        self._pending.append((mid, dict(body), delivery_count))

    def receive(self, *, max_messages: int = 1, max_wait: float | None = None) -> list:
        out = []
        while self._pending and len(out) < max_messages:
            mid, body, count = self._pending.pop(0)
            out.append(QueueMessage(body=body, message_id=mid, delivery_count=count))
        return out

    def complete(self, msg: QueueMessage) -> None:
        self.completed.append(msg)

    def abandon(self, msg: QueueMessage) -> None:
        self.abandoned.append(msg)
        if msg.delivery_count >= self.max_delivery_count:
            # broker 语义：投递耗尽 → 死信（不再回队）
            self.dlq.append(msg)
            return
        self._pending.append((msg.message_id, msg.body, msg.delivery_count + 1))
