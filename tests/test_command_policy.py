from personal_agent.commands.policy import CommandExecutionPolicy, command_execution_policy


def test_command_execution_lanes():
    assert command_execution_policy("hello") is None
    assert command_execution_policy("/stop") == CommandExecutionPolicy.CONTROL
    assert command_execution_policy("/steer focus") == CommandExecutionPolicy.CONTROL
    assert command_execution_policy("/mode local-auto") == CommandExecutionPolicy.NEXT_TURN
    assert command_execution_policy("/help") == CommandExecutionPolicy.SNAPSHOT
    assert command_execution_policy("/session list") == CommandExecutionPolicy.SNAPSHOT
    assert command_execution_policy("/session delete old") == CommandExecutionPolicy.BARRIER
    assert command_execution_policy("/memory delete id") == CommandExecutionPolicy.BARRIER
    assert command_execution_policy("/some-skill") == CommandExecutionPolicy.BARRIER
