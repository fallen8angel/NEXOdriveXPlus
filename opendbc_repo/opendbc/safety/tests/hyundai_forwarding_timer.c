// Exercise the production safety hooks without Python CANPacket bitfield ABI assumptions.
#include <stdbool.h>
#include <string.h>

#include "fake_stm.h"
#include "can.h"
#include "faults.h"

// Firmware logging function not supplied by fake_stm.h.
void putui(uint32_t value) {
  UNUSED(value);
}

// Board-owned flag used by the existing CAN-FD buffered forwarding path.
bool safety_tx_buffered_for_fwd = false;

#include "safety.h"

#ifdef _WIN32
#define TEST_EXPORT __declspec(dllexport)
#else
#define TEST_EXPORT __attribute__((visibility("default")))
#endif

static CANPacket_t test_packet(int addr, int bus, int length, const uint8_t *data) {
  CANPacket_t packet = {0};
  packet.addr = addr;
  packet.bus = bus;
  for (unsigned int dlc = 0U; dlc < sizeof(dlc_to_len); dlc++) {
    if (dlc_to_len[dlc] == length) {
      packet.data_len_code = dlc;
      break;
    }
  }
  memcpy(packet.data, data, length);
  return packet;
}

TEST_EXPORT int timer_test_init(uint16_t mode, uint16_t param) {
  return set_safety_hooks(mode, param);
}

TEST_EXPORT void timer_test_clock(uint32_t micros, uint32_t seconds) {
  timer.CNT = micros;
  safety_mode_cnt = seconds;
}

TEST_EXPORT int timer_test_tx(int addr, int bus, int length, const uint8_t *data) {
  CANPacket_t packet = test_packet(addr, bus, length, data);
  return safety_tx_hook(&packet);
}

TEST_EXPORT int timer_test_fwd(int addr, int bus, int length, uint8_t *data) {
  CANPacket_t packet = test_packet(addr, bus, length, data);
  int result = safety_fwd_hook(&packet);
  memcpy(data, packet.data, length);
  return result;
}

TEST_EXPORT void timer_test_relay(bool malfunction) {
  relay_malfunction = malfunction;
}

TEST_EXPORT void timer_test_controls(bool allowed, bool brake) {
  controls_allowed = allowed;
  brake_pressed = brake;
}

TEST_EXPORT void timer_test_button(int button, bool main) {
  hyundai_common_cruise_buttons_check(button, main);
}

TEST_EXPORT bool timer_test_controls_allowed(void) {
  return controls_allowed;
}
