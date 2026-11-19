# 人工采集说明

**当前稳定发行版为 0.6.3。** 该 ZIP 和 EXE 早于逐流开关与 Video-only。以下说明适用于带这些功能的源码版本，开录前请确认所用版本和界面。便携版应解压完整目录并直接双击 SynaerisCollector.exe。Windows 提示 UAC 时按正常授权流程处理。 [0.6.3 发行记录](https://github.com/Stellaria-Studio/Stellaria-Synaeris-Collector/releases/tag/v0.6.3)

## 开始前选择采集流

1. 在主界面查看“本次启用”的采集流。四个开关可独立选择，分别控制键鼠（含 Raw Input）、BetterGI 遥测、性能采样与 OCR。画面、帧序号和采集时钟是基本录制流。
2. 只允许录画面时，点 **Video-only**。确认四个开关均未勾选，界面显示“Video-only · 画面与帧时间索引”。这个预设关闭键鼠钩子和 Raw Input，跳过遥测端口检查与监听，关闭性能线程及进程采样，也不启动 OCR。
3. 核对工具版本、保存位置以及四项选择符合本次获准范围。界面反映本次程序配置，项目或账号的许可仍需另行确认。选择超出范围、无法确认录制配置或程序缺少开关时，先停止准备。
4. 在获准开展实际采集时，点 **开始采集** 并将游戏置于前台。键鼠关闭时程序不会用键鼠完整性预检查阻止视频录制。要停止，请返回采集器点击 **停止并保存**。游戏焦点内的 F10 与 F6–F9 全局快捷键需要开启键鼠流。

首次使用的新人工配置和无法读取旧偏好时使用 Video-only。已有有效配置缺少新字段时沿用老版本的默认行为，并显示具体选择。保存偏好和“恢复统一默认参数”会保留四项开关的取值。使用源码 CLI 时，`record --video-only` 关闭全部可选流，也可分别加 `--no-input`、`--no-telemetry`、`--no-performance` 或 `--no-ocr`。使用 FFmpeg 软件编码时在高级设置选择 `libx264`。性能开关控制采样线程，编码器设置独立。

## 封存后核对

- 在最近录像目录打开 `manifest.json`。`config` 记录输入配置。`capture_streams.sources` 逐项记录 `requested`、`started`、`status` 和 `reason`。Video-only 的画面应显示已启动，四个可选源均应是 `requested=false`、`started=false`、`status=disabled`。`not_started` 表示尚未启动，`unavailable` 表示启动、运行或关闭异常，或所需来源不可用。`enabled` 表示生产者已启动，结合 `counts`、实际事件和画面检查覆盖率。
- 允许性能采样时，核对性能采样源的 `sample_coverage` 和 `events.parquet`。出现 `partial` 或 `unavailable` 时，按事件时间和后续成功样本定位缺口。`no_samples` 表示未获得样本，旧 Manifest 缺少此字段时覆盖状态未知。需要该流的片段保留缺失标记。 [字段与判据](DATA_SCHEMA.md)
- Video-only 的输入、遥测、性能和 OCR 对应流应无记录。旧的 Parquet 文件仍保留空表以便兼容。帧焦点与时间映射用于视频审计。禁采输入会在索引中显示未采集，候选 DecisionUnit 保持未知。在日志、Manifest 或实际文件不符合预期时停止该配置的后续采集并保留原始记录。
- 完整视频、同步事件和候选旁路默认存于 `%LOCALAPPDATA%\Stellaria Synaeris\data\episodes`。上游更新只走独立缓存与下次启动机制。自动 OCR、Goal、场景和效果始终是候选，历史工具状态与奖励不构成任务结果。

本功能的验证范围是 Windows 源码测试、受控资源负控和非游戏 CPU 合成录像。实际游戏 WGC、焦点、HUD 可读性、本机权限组合和长时间运行需在获准环境中实测。
