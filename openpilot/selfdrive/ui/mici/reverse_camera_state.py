def should_show_reverse_camera(enabled: bool, started: bool, reverse_selected: bool) -> bool:
  return bool(enabled and started and reverse_selected)


def reverse_camera_action(requested: bool, has_dialog: bool, in_stack: bool, closing: bool) -> str:
  if closing:
    return "wait"
  if requested:
    return "create" if not has_dialog else ("wait" if in_stack else "push")
  return "dismiss" if has_dialog and in_stack else ("close" if has_dialog else "wait")
