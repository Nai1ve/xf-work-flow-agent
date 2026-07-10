# Meetingroom Policy

- Static room data helps normalize location and room references, but availability and conflicts must come from meetingroom tools.
- room_id/order_id used in write operations must come from current tool evidence or explicit user text verified by tools.
- booking.create requires a selected room candidate, day, start, end, and title.
- Existing booking operations must first identify the target booking with booking.list.
