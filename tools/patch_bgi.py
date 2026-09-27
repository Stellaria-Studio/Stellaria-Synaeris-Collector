"""Apply reviewed opt-in hooks to a pinned local BetterGI checkout, never installed binaries."""
import argparse
from pathlib import Path
import shutil
import subprocess

PIN = "42e1c0e745670eb4443c1e0357fba963eb24dfcd"
CALL = "global::BetterGenshinImpact.Core.Telemetry.SynaerisTelemetry"


def input_hooks(root, project):
    simulator = root / "Fischless.WindowsInput" / "InputSimulator.cs"
    if "InputDispatched" not in simulator.read_text(encoding="utf-8-sig"):
        replace(simulator, "public class InputSimulator : IInputSimulator\n{", '''public class InputSimulator : IInputSimulator
{
    // Optional read-only observer. Exceptions must never alter input dispatch.
    public static event Action<Vanara.PInvoke.User32.INPUT[], uint>? InputDispatched;
    internal static void ObserveDispatch(Vanara.PInvoke.User32.INPUT[] inputs, uint count)
    {
        try { InputDispatched?.Invoke(inputs, count); } catch { }
    }''')
        dispatcher = root / "Fischless.WindowsInput" / "WindowsInputMessageDispatcher.cs"
        anchor = "        uint num = User32.SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(User32.INPUT)));"
        replace(dispatcher, anchor, anchor + "\n        InputSimulator.ObserveDispatch(inputs, num);")
    simulation = project / "Core" / "Simulator" / "Simulation.cs"
    if '"BGI_INPUT_DISPATCH"' in simulation.read_text(encoding="utf-8-sig"):
        text = simulation.read_text(encoding="utf-8-sig")
        old = "mouse_data = mouse ? input.mi.mouseData : (uint?)null"
        if old in text:
            replace(simulation, old, "mouse_data = mouse ? input.mi.mouseData : (int?)null")
        return
    replace(simulation, "public class Simulation\n{", f'''public class Simulation
{{
    static Simulation()
    {{
        InputSimulator.InputDispatched += (inputs, count) =>
        {{
            if (!{CALL}.Enabled) return;
            for (int index = 0; index < inputs.Length; index++)
            {{
                var input = inputs[index];
                bool mouse = input.type == User32.INPUTTYPE.INPUT_MOUSE;
                bool keyboard = input.type == User32.INPUTTYPE.INPUT_KEYBOARD;
                {CALL}.Emit("input", "BGI_INPUT_DISPATCH", new {{
                    input_type = input.type.ToString(), batch_index = index,
                    batch_size = inputs.Length, dispatched_count = count,
                    dispatch_accepted = count == inputs.Length,
                    game_effect_verified = false, observation_point = "after_SendInput_return",
                    dx = mouse ? input.mi.dx : (int?)null,
                    dy = mouse ? input.mi.dy : (int?)null,
                    mouse_flags = mouse ? (uint)input.mi.dwFlags : (uint?)null,
                    mouse_data = mouse ? input.mi.mouseData : (int?)null,
                    mouse_unit = mouse ? (((uint)input.mi.dwFlags & 0x8000) != 0 ? "normalized_0_65535" : "SendInput_relative_units") : null,
                    vk = keyboard ? input.ki.wVk : (ushort?)null,
                    scan = keyboard ? input.ki.wScan : (ushort?)null,
                    key_flags = keyboard ? (uint)input.ki.dwFlags : (uint?)null
                }});
            }}
        }};
    }}''')


def telemetry_ui_hooks(project):
    dispatcher = project / "GameTask" / "TaskTriggerDispatcher.cs"
    text = dispatcher.read_text(encoding="utf-8-sig")
    old = f'''                        if (_frameIndex % 4 == 0)
                            {CALL}.Emit("ui", "UI_STATE", new {{ mode = content.CurrentGameUiCategory.ToString(), weak_label = true }});'''
    if old in text:
        replace(dispatcher, old, f'''                        if (_frameIndex % 4 == 0)
                        {{
                            // Trigger classification intentionally omits Main. Telemetry uses the
                            // cached Paimon template on the same image; never change trigger state.
                            var telemetryUi = content.CurrentGameUiCategory;
                            if (telemetryUi == global::BetterGenshinImpact.GameTask.Common.BgiVision.GameUiCategory.Unknown && Bv.IsInMainUi(content.CaptureRectArea))
                                telemetryUi = global::BetterGenshinImpact.GameTask.Common.BgiVision.GameUiCategory.Main;
                            {CALL}.Emit("ui", "UI_STATE", new {{ mode = telemetryUi.ToString(),
                                classification_source = "existing_bgi_templates", weak_label = true }});
                        }}''')
    # IsInMainUi can run full-half-screen OCR when a confirmation template exists.
    # This read-only bridge must not introduce that extra recognition workload.
    old_main = '''                            if (telemetryUi == global::BetterGenshinImpact.GameTask.Common.BgiVision.GameUiCategory.Unknown && Bv.IsInMainUi(content.CaptureRectArea))
                                telemetryUi = global::BetterGenshinImpact.GameTask.Common.BgiVision.GameUiCategory.Main;'''
    if old_main in dispatcher.read_text(encoding="utf-8-sig"):
        replace(dispatcher, old_main, '''                            if (telemetryUi == global::BetterGenshinImpact.GameTask.Common.BgiVision.GameUiCategory.Unknown)
                            {
                                using var paimonHint = content.CaptureRectArea.Find(global::BetterGenshinImpact.GameTask.Common.Element.Assets.ElementRecognition.Get("PaimonMenu", content.CaptureRectArea));
                                using var confirmHint = content.CaptureRectArea.Find(global::BetterGenshinImpact.Core.Recognition.RecognitionAssets.Get("AutoFight", "Confirm", content.CaptureRectArea));
                                if (paimonHint.IsExist() && confirmHint.IsEmpty())
                                    telemetryUi = global::BetterGenshinImpact.GameTask.Common.BgiVision.GameUiCategory.Main;
                            }''')


def native_group_hooks(project):
    group = project / "Core" / "Script" / "Group" / "ScriptGroupProject.cs"
    if 'implementation = "native_group"' in group.read_text(encoding="utf-8-sig"):
        return
    replace(group, "            await pathingTask.Pathing(task);", f'''            var telemetryId = Guid.NewGuid().ToString("N");
            {CALL}.Emit("tools", "TOOL_START", new {{ tool_id = telemetryId, tool = "Pathing",
                implementation = "native_group", group = GroupInfo?.Name, route = Name, folder = FolderName }});
            try
            {{
                await pathingTask.Pathing(task);
                {CALL}.Emit("tools", "TOOL_END", new {{ tool_id = telemetryId, tool = "Pathing",
                    outcome = pathingTask.SuccessEnd ? "executor_completed" : "unknown", verified = false,
                    reason = "executor_flag_is_not_quest_success" }});
            }}
            catch (Exception error)
            {{
                {CALL}.Emit("tools", "TOOL_FAILED", new {{ tool_id = telemetryId, tool = "Pathing",
                    outcome = "failure", verified = false, reason = error.Message }});
                throw;
            }}''')


def autofight_hooks(project):
    task = project / "GameTask" / "AutoFight" / "AutoFightTask.cs"
    if 'implementation = "native_autofight"' in task.read_text(encoding="utf-8-sig"):
        return
    replace(task, '''    public async Task Start(CancellationToken ct)
    {
        _ct = ct;''', f'''    public async Task Start(CancellationToken ct)
    {{
        var telemetryId = Guid.NewGuid().ToString("N");
        var telemetryTerminal = false;
        {CALL}.Emit("tools", "TOOL_START", new {{ tool_id = telemetryId, tool = "AutoFight",
            implementation = "native_autofight",
            strategy = System.IO.Path.GetFileNameWithoutExtension(_taskParam.CombatStrategyPath) }});
        _ct = ct;''')
    replace(task, '''        finally
        {
            AvatarRecognition.ClearCurrentAutoFightParam();
        }
    }

    private void LogScreenResolution()''', f'''        catch (OperationCanceledException)
        {{
            telemetryTerminal = true;
            {CALL}.Emit("tools", "TOOL_CANCELLED", new {{ tool_id = telemetryId, tool = "AutoFight",
                implementation = "native_autofight", outcome = "cancelled", verified = false }});
            throw;
        }}
        catch (Exception error)
        {{
            telemetryTerminal = true;
            {CALL}.Emit("tools", "TOOL_FAILED", new {{ tool_id = telemetryId, tool = "AutoFight",
                implementation = "native_autofight", outcome = "failure", verified = false,
                reason = error.Message }});
            throw;
        }}
        finally
        {{
            AvatarRecognition.ClearCurrentAutoFightParam();
            if (!telemetryTerminal)
                {CALL}.Emit("tools", "TOOL_END", new {{ tool_id = telemetryId, tool = "AutoFight",
                    implementation = "native_autofight", outcome = "executor_completed", verified = false,
                    reason = "combat_task_return_does_not_authenticate_enemy_defeat_or_rewards" }});
        }}
    }}

    private void LogScreenResolution()''')


def detail_hooks(project):
    executor = project / "GameTask" / "AutoPathing" / "PathExecutor.cs"
    anchor = "                        CurWaypoint = (waypoints.FindIndex(wps => wps == waypoint), waypoint);"
    if '"PATHING_WAYPOINT"' not in executor.read_text(encoding="utf-8-sig"):
        replace(executor, anchor, anchor + f'''
                        {CALL}.Emit("tools", "PATHING_WAYPOINT", new {{
                            waypoint_index = CurWaypoint.Item1, target_x = waypoint.X, target_y = waypoint.Y,
                            type = waypoint.Type, move_mode = waypoint.MoveMode, action = waypoint.Action,
                            action_params = waypoint.ActionParams, reached = false, weak_label = true }});''')
    pick = project / "GameTask" / "AutoPick" / "AutoPickTrigger.cs"
    anchor = '            _logger.LogInformation("交互或拾取：{Text}", text);'
    if '"INTERACTION_ATTEMPT"' not in pick.read_text(encoding="utf-8-sig"):
        replace(pick, anchor, anchor + f'''
            {CALL}.Emit("objects", "INTERACTION_ATTEMPT", new {{
                target_text = text, source_frame_index = content.FrameIndex, verified = false,
                bbox = (object?)null, world_position = (object?)null, weak_label = true }});''')


def replace(path, old, new, expected=1):
    text = path.read_text(encoding="utf-8-sig")
    if text.count(old) != expected:
        raise RuntimeError(f"Upstream hook drift: {path}: expected {expected}, found {text.count(old)}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("checkout")
    parser.add_argument("--upgrade-existing", action="store_true")
    args = parser.parse_args()
    root = Path(args.checkout).resolve()
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if head != PIN:
        raise RuntimeError("BetterGI revision changed; inspect hooks before updating PIN")
    if subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True).strip() and not args.upgrade_existing:
        raise RuntimeError("BetterGI checkout must be clean before patching")
    project = root / "BetterGenshinImpact"
    telemetry = project / "Core" / "Telemetry" / "SynaerisTelemetry.cs"
    telemetry.parent.mkdir(exist_ok=True)
    shutil.copyfile(Path(__file__).parent.parent / "integrations" / "bettergi" / "SynaerisTelemetry.cs", telemetry)
    if args.upgrade_existing:
        native_group_hooks(project)
        autofight_hooks(project)
        detail_hooks(project)
        input_hooks(root, project)
        telemetry_ui_hooks(project)
        patch = subprocess.check_output(["git", "-C", str(root), "diff", "--binary"])
        destination = Path(__file__).parent.parent / "integrations" / "bettergi" / "readonly-telemetry.patch"
        destination.write_bytes(patch)
        print(destination)
        return
    dispatcher = project / "GameTask" / "TaskTriggerDispatcher.cs"
    anchor = "content.CurrentGameUiCategory = Bv.WhichGameUiForTriggers(content.CaptureRectArea);"
    replace(dispatcher, anchor, anchor + f'''
                        if (content.FrameIndex % 4 == 0)
                            {CALL}.Emit("ui", "UI_STATE", new {{ mode = content.CurrentGameUiCategory.ToString(), weak_label = true }});''')
    # Avoid assumptions about CaptureContent frame-index API: use dispatcher's own field.
    replace(dispatcher, "content.FrameIndex % 4", "_frameIndex % 4")
    anchor = "trigger.OnCapture(content);"
    replace(dispatcher, anchor, anchor + f'''
                                if (_frameIndex % 4 == 0)
                                    {CALL}.Emit("tools", "TRIGGER_STATE", new {{ tool = trigger.Name, enabled = trigger.IsEnabled, exclusive = trigger.IsExclusive }});''')
    navigation = project / "GameTask" / "AutoPathing" / "NavigationInstance.cs"
    anchor = '        WeakReferenceMessenger.Default.Send(new PropertyChangedMessage<object>(typeof(Navigation),'
    replace(navigation, anchor, f'''        {CALL}.Emit("pose", "POSE", new {{ x = p.X, y = p.Y, yaw = (double?)null,
            valid = p != default, confidence = (double?)null, map = mapName, coordinate_frame = "bgi_map_pixels", weak_label = true }});
''' + anchor, expected=2)
    image = project / "GameTask" / "Model" / "Area" / "ImageRegion.cs"
    anchor = "            var result = OcrFactory.Paddle.OcrResult(roi);"
    replace(image, anchor, anchor + f'''
            if ({CALL}.Enabled)
            {{
                try
                {{
                    foreach (var entry in result.Regions)
                    {{
                        var bounds = entry.Rect.BoundingRect() + effectiveRegionOfInterest.Location;
                        bounds = ConvertPositionToGameCaptureRegion(bounds.X, bounds.Y, bounds.Width, bounds.Height);
                        {CALL}.Emit("ocr", "OCR_TEXT", new {{ text = entry.Text,
                            bbox = new[] {{ new[] {{ bounds.X, bounds.Y }}, new[] {{ bounds.Right, bounds.Y }},
                                new[] {{ bounds.Right, bounds.Bottom }}, new[] {{ bounds.X, bounds.Bottom }} }},
                            confidence = entry.Score, source = "bettergi_ocr", version = global::BetterGenshinImpact.Core.Config.Global.Version, weak_label = true }});
                    }}
                }}
                catch {{ /* Telemetry cannot alter recognition behavior. */ }}
            }}''', expected=3)
    pathing = project / "Core" / "Script" / "Dependence" / "AutoPathingScript.cs"
    replace(pathing, "        try\n        {\n            var task = PathingTask.BuildFromJson(json);", f'''
        var telemetryId = Guid.NewGuid().ToString("N");
        {CALL}.Emit("tools", "TOOL_START", new {{ tool_id = telemetryId, tool = "Pathing", implementation = "native" }});
        try
        {{
            var task = PathingTask.BuildFromJson(json);''')
    replace(pathing, "            await pathExecutor.Pathing(task);", f'''            await pathExecutor.Pathing(task);
            {CALL}.Emit("tools", "TOOL_END", new {{ tool_id = telemetryId, tool = "Pathing",
                outcome = pathExecutor.SuccessEnd ? "executor_completed" : "unknown", verified = false,
                reason = "executor_flag_is_not_quest_success" }});''')
    replace(pathing, '            TaskControl.Logger.LogDebug(e,"执行地图追踪时候发生错误");', f'''            {CALL}.Emit("tools", "TOOL_FAILED", new {{ tool_id = telemetryId, tool = "Pathing",
                outcome = "failure", verified = false, reason = e.Message }});
            TaskControl.Logger.LogDebug(e,"执行地图追踪时候发生错误");''')
    native_group_hooks(project)
    autofight_hooks(project)
    detail_hooks(project)
    input_hooks(root, project)
    telemetry_ui_hooks(project)
    subprocess.run(["git", "-C", str(root), "add", "-N", str(telemetry.relative_to(root))], check=True)
    patch = subprocess.check_output(["git", "-C", str(root), "diff", "--binary"])
    destination = Path(__file__).parent.parent / "integrations" / "bettergi" / "readonly-telemetry.patch"
    destination.write_bytes(patch)
    print(destination)


if __name__ == "__main__":
    main()
