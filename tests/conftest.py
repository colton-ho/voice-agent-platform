import os

# B12 fail-fast（AUTH_SECRET 缺值拒启）下的测试缝：单测进程一律有值。
# 缺值拒启行为本身由 test_startup_secrets.test_auth_secret_fail_fast_when_missing 钉住。
os.environ.setdefault("AUTH_SECRET", "test-secret")
