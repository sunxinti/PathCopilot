CREATE TRIGGER IF NOT EXISTS tr_agent_messages_immutable_content
BEFORE UPDATE ON runtime_agent_messages
WHEN
    NEW.message_id IS NOT OLD.message_id
    OR NEW.run_id IS NOT OLD.run_id
    OR NEW.task_id IS NOT OLD.task_id
    OR NEW.sender_task_id IS NOT OLD.sender_task_id
    OR NEW.recipient_task_id IS NOT OLD.recipient_task_id
    OR NEW.role IS NOT OLD.role
    OR NEW.kind IS NOT OLD.kind
    OR NEW.content_text IS NOT OLD.content_text
    OR NEW.payload_json IS NOT OLD.payload_json
    OR NEW.content_hash IS NOT OLD.content_hash
    OR NEW.idempotency_key IS NOT OLD.idempotency_key
    OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'Agent message content is immutable');
END;

CREATE TRIGGER IF NOT EXISTS tr_agent_tool_calls_immutable_identity
BEFORE UPDATE ON runtime_agent_tool_calls
WHEN
    NEW.tool_call_id IS NOT OLD.tool_call_id
    OR NEW.run_id IS NOT OLD.run_id
    OR NEW.task_id IS NOT OLD.task_id
    OR NEW.name IS NOT OLD.name
    OR NEW.arguments_json IS NOT OLD.arguments_json
    OR NEW.idempotency_key IS NOT OLD.idempotency_key
    OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'Agent tool-call identity is immutable');
END;
