"""Service Bus 队列解析缝（pipeline-infra-spec §2.1 单队列 postcall-jobs）。

生产：DefaultAzureCredential + SERVICEBUS_NAMESPACE（B-5 凭据接管），
SDK 全程惰性导入——未配置队列时 bot 可照常 import（in-process 降级路径）。
测试：set_queue_provider 注入 in-memory 队列，不触 SDK。

队列操作是本仓自定义的窄接口（4 个动词 send/receive/complete/abandon），
真实适配层 _RealQueue 包 azure-servicebus 的 peek-lock 语义；消息统一为
QueueMessage（body/message_id/delivery_count），业务层与 SDK 解耦。
"""
import json
import os

# §2.1 单队列（job_type 判别；命名冻结，不设 env 变量——不新造契约面）
POSTCALL_QUEUE = "postcall-jobs"

_provider_override = None
_real_queues: dict = {}


class QueueMessage:
    """peek-lock 收到的消息（窄接口载体）。

    delivery_count 语义见 workers.postcall_worker.is_final_delivery——
    AMQP「既往投递次数」（首次=0）与 Azure 文档「times delivered」措辞并存，
    消费侧按双约定安全侧判定最后一次投递。
    """

    def __init__(self, body: dict, message_id: str, delivery_count: int, raw=None):
        self.body = body
        self.message_id = message_id
        self.delivery_count = delivery_count
        self._raw = raw


def set_queue_provider(provider) -> None:
    """测试缝：provider(queue_name) -> 队列对象（4 动词）。"""
    global _provider_override
    _provider_override = provider


def reset_queue_provider() -> None:
    global _provider_override
    _provider_override = None


def get_queue(name: str):
    if _provider_override is not None:
        return _provider_override(name)
    return _real_queue(name)


def _real_queue(name: str):
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient

    namespace = os.environ.get("SERVICEBUS_NAMESPACE", "").strip()
    if not namespace:
        raise RuntimeError("SERVICEBUS_NAMESPACE not configured")
    if name not in _real_queues:
        client = ServiceBusClient(
            f"{namespace}.servicebus.windows.net",
            credential=DefaultAzureCredential(),
        )
        _real_queues[name] = _RealQueue(client, name)
    return _real_queues[name]


class _RealQueue:
    """azure-servicebus 队列 → 窄接口。send 同步；receive peek-lock，
    complete/abandon 经同一长驻 receiver 结算（锁有效期内）。"""

    def __init__(self, client, name: str):
        self._client = client
        self._name = name
        self._sender = None
        self._receiver = None

    def send(self, *, message_id: str, body: dict) -> None:
        from azure.servicebus import ServiceBusMessage

        if self._sender is None:
            self._sender = self._client.get_queue_sender(queue_name=self._name)
            self._sender.__enter__()
        self._sender.send_messages(
            ServiceBusMessage(json.dumps(body), message_id=message_id)
        )

    def receive(self, *, max_messages: int = 1, max_wait: float | None = None) -> list:
        if self._receiver is None:
            self._receiver = self._client.get_queue_receiver(queue_name=self._name)
            self._receiver.__enter__()
        msgs = self._receiver.receive_messages(
            max_message_count=max_messages,
            max_wait_time=max_wait,
        )
        out = []
        for m in msgs:
            body = json.loads(str(m))
            out.append(
                QueueMessage(
                    body=body,
                    message_id=m.message_id,
                    delivery_count=int(getattr(m, "delivery_count", 0) or 0),
                    raw=m,
                )
            )
        return out

    def complete(self, msg: QueueMessage) -> None:
        self._receiver.complete_message(msg._raw)

    def abandon(self, msg: QueueMessage) -> None:
        self._receiver.abandon_message(msg._raw)
