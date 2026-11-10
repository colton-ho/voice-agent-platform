# 自测记录 · U-103 语音纵向切片（本地）——bot.py 组合根接线 + cascade 路径 + 四演示

对应冻结卡：implementation-plan.md §5【U-103】（bot.py composition-root wiring behind
explicit registry selection；`VOICE_ENGINE=cascade` path STT→LLM→TTS streaming
（phrase-level, spec §7.1）；local streaming/interruption/lifecycle-cleanup/
idempotent-tool demo per spec Phase B completion；existing store/session modules
untouched）。自测环境：pinned venv（Python 3.13.12），pytest `--capture=sys`，
basetemp mv dodge，零真实凭据/网络。

## 一、验收标准逐条自测

| # | 验收标准 | 自测方法 | 结果 | 证据 |
|---|---|---|---|---|
| 1 | bot.py 组合根接线：显式 registry 选型，`load_config` 成为真启动校验（违规一次性全报） | tests/test_composition_root.py::test_load_config_is_startup_validation（空环境 → ConfigError，含 VOICE_ENGINE/STT_PROVIDER/LLM_ENDPOINT/COSMOS_ENDPOINT 等多变量同报 + 占位符含义） | ✅ | 4 passed；bot.py `build_cascade_engine` = load_config → registry → 三元组 → CascadeEngine |
| 2 | registry 显式选型组出 FC-2/3/4 三元组（§11→FC-0 采样率映射、U-101 voice_lookup 注入缝） | test_registry_selected_adapters_wired（azure_speech×2 + azure_openai；输入 16 kHz / 合成 24 kHz）；test_minimax_tts_selectable_with_voice_lookup_seam（TTS_PROVIDER=minimax_tts 可选，voice_lookup 接 bot.voices_for） | ✅ | 类名断言 + providers.base ABC isinstance（规避 test_minimax_tts reload 双类对象坑） |
| 3 | 零默认零回退（R-C-002）：遗留名永不解析 | test_legacy_aws_names_never_resolve（transcribe / bedrock / nova-sonic 三个方向） | ✅ | 3× UnsupportedProviderError |
| 4 | `VOICE_ENGINE=cascade` 路径 STT→LLM→TTS 短语级流式（spec §7.1：不等整段回答） | test_local_streaming_phrase_level_tts：5 句短语逐段喂 TTS，事件布局首块音频（idx 4）先于末句 delta（idx 9）先于回合 end；tts.texts == 5 个句末短语（有界） | ✅ | 事件序：interim→final→delta/chunk 交错→usage→chunk(is_final)→end |
| 5 | 演示 ①本地流式（同 #4）+ FC-4 契约（is_final 只落终块） | 同上：chunks is_final == [F,F,F,F,T]，audio 全非空，回合单 end 收尾 | ✅ | test_cascade_engine.py |
| 6 | 演示 ②打断（R-P-009）：LLM+TTS 取消，产出即刻停止，会话可复用 | test_interruption_cancels_llm_and_tts：interrupt() 后 llm.cancel+tts.cancel 各≥1，事件计数 0.25s 内零增长，作废回合不发 end，新回合正常收尾 | ✅ | 另有自动 barge-in：test_new_final_transcript_barges_in_active_turn（新 final 即作废旧回合） |
| 7 | 演示 ③生命周期清理：close 幂等、stt.stop、在途回合被取消 | test_lifecycle_cleanup_idempotent：close→stt.stopped==1、双 cancel≥1、二次 close 不抛不重复、事件出口关闭、close 后 push_audio 拒绝 | ✅ | test_cascade_engine.py |
| 8 | 演示 ④幂等工具：同 ToolRequest.id 只执行一次 | test_tool_requests_execute_once_per_id：重复 call_1 → executed==["call_1"]，两轮回执均按 U-102 钉死形状回灌（assistant 正文 + role="tool" 回执） | ✅ | 3 轮 LLM 调用，消息形状逐轮断言 |
| 9 | R-A-005 验收：各阶段超时/取消路径确定性测试（stt.start / LLM 事件步 / TTS 音频块步） | test_stt_start_timeout / test_llm_event_timeout_cancels_llm / test_tts_event_timeout_cancels_tts（各配 cancel 断言 + on_turn_error(stage) 观测） | ✅ | 3 passed |
| 10 | spec §7.1：显式识别语言，code-switching 不假设、配置错误不静默接受 | test_explicit_language_required（空 language → ValueError，stt 未启动） | ✅ | 引擎 start() 硬拒绝空语言并文档化检测行为 |
| 11 | bot.py:724 失实注释改诚实（chunk_size = sample_rate×0.5s×2，1.4.0 实际语义） | 目视 diff + 批次回归 | ✅ | bot.py:724-728 新注释（0.5s/块=24000 字节@24kHz；`or 960` 仅历史兜底），行为未动 |
| 12 | R-P-001/004/009 保持：现有路径行为不变（L1–L3 Bedrock 调用面不拆） | 全量回归 + git status：现有 46 测试文件零改动（Step 1 两文件除外），_build_pipeline/_build_tts/nova 分支零改动 | ✅ | 6 红全部为既定基準（见 §四），零新增红 |
| 13 | store/session 模块不动；FC-0..FC-8 冻结；models[] 不动 | git status 清单核对 | ✅ | providers/base.py、providers/registry.py、MODELS/voices 常量、user_store/demo_loader/runtime_config 等均未触碰 |
| 14 | requirements.txt/.lock 变更需报 what/why | 无变更 | ✅ | cascade_engine.py 仅 stdlib + providers.base；测试零新依赖 |

## 二、异常场景自测

| 场景 | 期望 | 结果 |
|---|---|---|
| LLM 流挂起（事件步超时） | llm_event_timeout 触发 → llm.cancel()、on_turn_error("llm", TimeoutError)、回合 end 收尾 | ✅ |
| TTS 挂起（音频块步超时） | tts_event_timeout 触发 → tts.cancel()、on_turn_error("tts", TimeoutError)、回合 end 收尾 | ✅ |
| stt.start 挂起 | stt_start_timeout → TimeoutError 上抛调用方 | ✅ |
| 工具请求到达但未注入 execute_tool | RuntimeError 响亮（执行确认缝缺失不静默） | ✅（代码路径；工具演示覆盖正路径） |
| LLM 工具多轮（含重复 id） | 首次执行一次并缓存，重复 id 回放缓存；轮数受 max_tool_rounds 有界 | ✅ |
| close() 重复调用 / close 后 push_audio | 幂等不抛 / RuntimeError | ✅ |
| 遗留供应商名（transcribe/bedrock/nova-sonic，含大小写变体由 registry casefold 封杀） | UnsupportedProviderError（列已注册名单） | ✅ |
| §11 配置大面积缺失 | ConfigError 一次性列全部违规（指名变量+含义），非逐个试错 | ✅ |
| 测试隔离（test_minimax_tts reload 适配器模块） | 组合根断言走类名 + 契约 ABC，不再被双类对象假阴性命中 | ✅（首跑发现即修） |

## 三、涉及文件清单

- **新增**：`cascade_engine.py`（FC-2/3/4 级联引擎：短语切分、工具幂等、各阶段超时/取消）；`tests/test_cascade_engine.py`（11 测试，含 F-11 回收钉）；`tests/test_composition_root.py`（5 测试，含 F-12 行为钉）；本报告。
- **修改**：`bot.py` 两处——① :724-728 注释改诚实（行为未动）；② `_build_pipeline` 后新增组合根 `build_cascade_engine`（load_config 真启动校验 + registry 接线 + voices_for→minimax voice_lookup 注入缝 + execute_tool 执行确认缝）。
- **复用（确认零重复实现）**：`providers/base`（FC-0..FC-8 未动）、`providers/registry`（cascade 三元组语义原样消费）、`config_contract.load_config`、`providers/speech/azure_speech.register`、`providers/speech/minimax_tts.register`、`providers/llm/azure_openai.register`、`providers/speech/_tts_stream`（真实适配器内部，fake 只复刻其单块前瞻语义）、`bot.voices_for`。
- **未动**：store/session 模块（user_store、demo_loader、runtime_config、demo_store、voice_store、activity_store、history/mcp_config）、`requirements.txt`/`requirements.lock`、FC 契约、MODELS 选型常量、L1–L3 Bedrock 调用面（#16 重写面）。NOVA_SONIC_VOICES/LANGUAGES/ENGINES 选型面「未动」仅限 U-103 主体时点；裁决 B ①-④ 落地后已动（见 §四.1）。
- **承 Step 1 修复环**：`tests/test_mcp_sigv4.py`（F-08 回补 403 SignedHeaders 钉，1 passed）、`providers/speech/azure_speech.py`（F-09 删 6 未用导入）。
- **承裁决 B 后修复环（C2+F-11/F-12）**：`tests/test_tool_filler.py`/`tests/test_mcp_integration.py`（C2 构造补 `pipeline_worker`，fixture-only）、`cascade_engine.py`（F-11 start 超时/取消回收路径，唯一生产代码面）、`tests/test_cascade_engine.py`（F-11 回收测试 +1）、`tests/test_composition_root.py`（F-12 行为钉 +1）。

## 四、未覆盖项 / 遗留问题

1. **决策 (a) engine-set 范围：裁决 B 已下（team-lead 2026-11-09）、①-④ 已落地**：§7 決策 (a)「Consumed by: U-103 engine set」四项经裁决 B「①-④ 本步全做」执行完毕——①`ENGINES`→`[pipeline]`（`DEFAULT_ENGINE="pipeline"`）②PHONE_ENGINE 默认随 DEFAULT_ENGINE 落 pipeline ③NOVA_SONIC_VOICES/ALIASES 清空、DEFAULT_NOVA_SONIC_VOICE 失效（C-3，/api/config 键形状不变）④8 个 Nova-only locale `engines:[]`（Voice-Live-gated，条目本体不删）；nova-sonic 代码路径、models[]、voice_store/DB 零改动（AWS 回滚路完整）。测试断言重指 23 处/5 文件（test_nova_voices 只重指不删文件，退役仍归 U-501）。裁决引用：team-lead 裁决 B 2026-11-09（旗1 裁决维持 `engines:[]`；旗2 `_validate_segment` 预存边角登 #18+U-501，不另立卡不修）。
2. **cascade 路径未接 ws/phone 传输层**：卡片明示「Voice-call vertical slice (**local**)」「**local** ... demo」，Phase B completion = 本地演示；ws/phone 传输集成属 U-2xx（U-201..206 传输面）。若派令预期本步含 ws 路由钩子，请明示后再补。
3. **CR-C1 凭据占位符缺口**（既有）：api_key 形参即 Key Vault 接管点，本步不发明 §11 变量；**CR-C2**（pipecat-ai[azure] deploy pin）live wrap 留 U-501，bridge lazy-load 不变——均按派令未动。
4. 工具参数校验（FC-3 validate_tool_request 钩子）在引擎侧为条件调用（适配器带钩子才走）；失败走回合错误路径（不静默）。工具执行器注入后的真实业务工具联调属后续卡（R-P-004 工具面保持不动）。

## 五、命令与数字（全量归因）

| 命令（均 `--capture=sys` + basetemp mv dodge） | 结果 |
|---|---|
| `pytest tests/test_tool_filler.py tests/test_mcp_integration.py tests/test_cascade_engine.py tests/test_composition_root.py -q`（本修复环批次） | **43 passed** |
| `pytest tests/ -q`（全量） | **4 failed, 985 passed** |

全量归因：985 passed = 981（U-103 收口时点）+ 2（C2 转绿：test_tool_filler 集成钉、test_mcp_integration 工具往返）+ 2（本环新增：F-11 回收测试、F-12 行为钉）；4 failed 全部为既定基準红（C4×4）：test_demo_store×1、test_history×2、test_migrate_demos×1——**零新增红**（C2×2 转绿归零）。

U-103 收口时点历史快照（本修复环前）：全量 6 failed / 981 passed（C4×4+C2×2）；cascade+composition 两文件 14 passed；10 文件批 226 passed。

> 簿记补注（#18 全局一致性扫尾，2026-11-10）：上文 C4×4 为当时快照——其中 test_history finalize×2 为 C4 预存成员，已由 #17 续跑修法①销（现 C4×2，红签名逐字节不变；见 mapping §6「N2 C4 债务簿记」行）。
