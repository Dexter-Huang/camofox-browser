"""单窗口 VNC 发布器的协议稳定性测试。"""

from app.window_vnc import WindowPublisher


def test_x11vnc_command_disables_unstable_incremental_copy_and_idle_timeout() -> None:
    """动态网页必须使用可长期空闲的保守 RFB 参数，避免增量矩形流失步。"""
    publisher = WindowPublisher(
        display=":99",
        window_id="0x123",
        rfb_port=5902,
        expose_rfb_to_docker_network=True,
        capture_wait_ms=40,
        capture_defer_ms=40,
    )

    command = publisher.x11vnc_command()

    assert "-nowirecopyrect" in command
    assert "-noscrollcopyrect" in command
    assert command[command.index("-readtimeout") + 1] == "0"
    assert "-ping" not in command
