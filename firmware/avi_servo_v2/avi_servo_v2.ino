// AVI servo controller, firmware v2.
//
// Protocol, 115200 baud, newline-terminated (BUILD_SPEC 6.4):
//   P a0 a1 a2 a3   absolute target angles in degrees, sent ~50 Hz by the host.
//                   The firmware eases toward them under its own speed cap.
//   H               go home slowly.
//   ?               reply "S a0 a1 a2 a3 v2" with the angles it is holding now.
//   E               hold position and ignore P until R.
//   R               resume.
//
// Safety, all enforced here and NOT trusted to the host:
//   - every channel clamped to its bench-calibrated min/max
//   - per-joint speed cap
//   - watchdog: no P for 500 ms and it holds the last position
//   - it never moves on its own: nothing is written to the PCA9685 until the
//     first P or H arrives
//
// WHY THE STARTUP IS WRITTEN THIS WAY
// Opening the serial port resets the Uno. It does NOT reset the PCA9685, which
// keeps driving the servos from its own registers while it has power. So the
// arm is still where it was, and the old firmware's habit of snapping to
// 90/90 on boot was the sketch doing that, not the reset. v2 reads the OFF
// registers back over I2C and starts from where the arm actually is.
//
// If the read-back is implausible (I2C stalled, or the board was just powered
// on and the registers are zero) it falls back to waiting for the first P and
// ramping there at HOMING_DEG_S, which is slow enough to watch.
//
// BUILD_SPEC calls the read-back unverified, so verify it on the bench before
// relying on it: power the arm, move a joint with the host, reset the Uno, and
// check that MODE_* reports READBACK and the arm does not twitch.
//
// Fixed from v1: channel 1 was driven with the wrist's range (140/380/580)
// instead of the shoulder's (75/370/580).

#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

static const uint8_t  PCA_ADDR      = 0x40;
static const uint8_t  LED0_ON_L     = 0x06;
static const uint8_t  REG_MODE1     = 0x00;
static const uint8_t  REG_PRESCALE  = 0xFE;
static const uint16_t PWM_FREQ_HZ   = 50;
static const uint8_t  NJ            = 4;

static const float    SPEED_CAP_DEG_S = 60.0f;   // config/joints.toml meta
static const float    HOMING_DEG_S    = 18.0f;   // H, and the unknown-pose ramp
static const uint16_t WATCHDOG_MS     = 500;
static const uint8_t  TICK_MS         = 10;

// Bench calibration of 2026-08-04. MUST stay in step with config/joints.toml.
// min/center/max are raw PCA9685 counts; soft limits are degrees.
struct Joint {
  uint16_t minCount, centerCount, maxCount;
  float    softMin, softMax;
  bool     driven;
};

static const Joint JOINTS[NJ] = {
  //  min  center  max   softMin softMax driven
  {    75,    250,  460,   20.0f, 160.0f, true  },  // 0 base yaw
  {    75,    370,  580,   25.0f, 155.0f, true  },  // 1 shoulder  (v1 BUG here)
  {    80,    430,  570,   30.0f, 150.0f, false },  // 2 elbow, sign unknown
  {   140,    380,  580,   30.0f, 150.0f, false },  // 3 wrist, sign unknown
};

enum StartMode { MODE_UNKNOWN, MODE_READBACK };

static float      current[NJ];          // degrees we are actually outputting
static float      target[NJ];           // degrees we are easing toward
static bool       haveTarget = false;   // nothing is written before the first P
static bool       holding    = false;   // E / R
static bool       homing     = false;   // H uses the slower cap
static StartMode  startMode  = MODE_UNKNOWN;
static uint32_t   lastCmdMs  = 0;
static char       line[48];
static uint8_t    lineLen    = 0;

// --- the piecewise map, identical to config/joints.toml ----------------------
// 0 deg -> minCount, 90 -> centerCount, 180 -> maxCount. A degree is worth a
// different number of counts on each side of center, because the calibrated
// center is not the midpoint of the calibrated range.
static uint16_t degToCount(uint8_t j, float deg) {
  const Joint &J = JOINTS[j];
  if (deg < 0.0f)   deg = 0.0f;
  if (deg > 180.0f) deg = 180.0f;
  float c;
  if (deg <= 90.0f) {
    c = J.minCount + (float)(J.centerCount - J.minCount) * (deg / 90.0f);
  } else {
    c = J.centerCount + (float)(J.maxCount - J.centerCount) * ((deg - 90.0f) / 90.0f);
  }
  return (uint16_t)(c + 0.5f);
}

static float countToDeg(uint8_t j, uint16_t count) {
  const Joint &J = JOINTS[j];
  if (count <= J.centerCount) {
    if (J.centerCount == J.minCount) return 90.0f;
    return 90.0f * (float)(count - J.minCount) / (float)(J.centerCount - J.minCount);
  }
  if (J.maxCount == J.centerCount) return 90.0f;
  return 90.0f + 90.0f * (float)(count - J.centerCount) / (float)(J.maxCount - J.centerCount);
}

static float clampDeg(uint8_t j, float deg) {
  const Joint &J = JOINTS[j];
  if (deg < J.softMin) return J.softMin;
  if (deg > J.softMax) return J.softMax;
  return deg;
}

static uint8_t readReg(uint8_t reg) {
  Wire.beginTransmission(PCA_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission() != 0) return 0xFF;
  if (Wire.requestFrom(PCA_ADDR, (uint8_t)1) != 1) return 0xFF;
  return Wire.read();
}

// Setting the PWM frequency means putting the PCA9685 to SLEEP to load the
// prescaler, and sleeping it stops the outputs: for those few milliseconds the
// servos get no pulses and a loaded arm can sag. That is a startup jump by
// another route. So only set the frequency if it is not already right, which
// it will be whenever the chip kept power across an Uno reset.
static bool ensurePwmFreq() {
  // prescale = round(25 MHz / (4096 * freq)) - 1, the datasheet's formula.
  uint8_t want = (uint8_t)(25000000.0f / (4096.0f * (float)PWM_FREQ_HZ) + 0.5f) - 1;
  uint8_t have = readReg(REG_PRESCALE);
  uint8_t mode1 = readReg(REG_MODE1);
  bool asleep = (mode1 == 0xFF) || (mode1 & 0x10);
  if (have == want && !asleep) return false;   // already running at 50 Hz
  pwm.setPWMFreq(PWM_FREQ_HZ);
  return true;
}

// --- read the PCA9685's current output back ---------------------------------
// Returns 0xFFFF if the read failed or the value is outside this joint's
// calibrated range, which is the same thing as "do not trust it".
static uint16_t readOffCount(uint8_t ch) {
  Wire.beginTransmission(PCA_ADDR);
  Wire.write((uint8_t)(LED0_ON_L + 4 * ch + 2));   // LEDn_OFF_L
  if (Wire.endTransmission() != 0) return 0xFFFF;
  if (Wire.requestFrom(PCA_ADDR, (uint8_t)2) != 2) return 0xFFFF;
  uint16_t lo = Wire.read();
  uint16_t hi = Wire.read();
  if (hi & 0x10) return 0xFFFF;                    // bit 12 = full-off, not a position
  uint16_t count = ((hi & 0x0F) << 8) | lo;
  const Joint &J = JOINTS[ch];
  if (count < J.minCount || count > J.maxCount) return 0xFFFF;
  return count;
}

static void setup_start_pose() {
  bool all = true;
  float got[NJ];
  for (uint8_t j = 0; j < NJ; j++) {
    uint16_t c = readOffCount(j);
    if (c == 0xFFFF) { all = false; break; }
    got[j] = countToDeg(j, c);
  }
  if (all) {
    startMode = MODE_READBACK;
    for (uint8_t j = 0; j < NJ; j++) current[j] = clampDeg(j, got[j]);
  } else {
    // We do not know where the arm is. Assume center, which is also what a
    // cold power-on leaves, and wait for a P before moving at all. The first
    // move then creeps at HOMING_DEG_S instead of taking the full speed cap.
    startMode = MODE_UNKNOWN;
    for (uint8_t j = 0; j < NJ; j++) current[j] = 90.0f;
  }
  for (uint8_t j = 0; j < NJ; j++) target[j] = current[j];
}

// --- commands ---------------------------------------------------------------
static void handleP(char *args) {
  float v[NJ];
  uint8_t n = 0;
  char *tok = strtok(args, " \t");
  while (tok && n < NJ) {
    v[n++] = atof(tok);
    tok = strtok(NULL, " \t");
  }
  if (n != NJ) return;                   // a short or corrupt frame is ignored
  for (uint8_t j = 0; j < NJ; j++) {
    target[j] = JOINTS[j].driven ? clampDeg(j, v[j]) : 90.0f;
  }
  haveTarget = true;
  homing = false;
  lastCmdMs = millis();
}

static void handleLine() {
  if (lineLen == 0) return;
  line[lineLen] = '\0';
  switch (line[0]) {
    case 'P': handleP(line + 1); break;
    case 'H':
      for (uint8_t j = 0; j < NJ; j++) target[j] = 90.0f;
      haveTarget = true;
      homing = true;
      holding = false;
      lastCmdMs = millis();
      break;
    case 'E': holding = true; break;
    case 'R': holding = false; lastCmdMs = millis(); break;
    case '?':
      Serial.print('S');
      for (uint8_t j = 0; j < NJ; j++) {
        Serial.print(' ');
        Serial.print(current[j], 1);
      }
      Serial.println(F(" v2"));
      break;
    case 'M':                            // not in the spec; bench diagnostic
      Serial.print(F("M mode="));
      Serial.print(startMode == MODE_READBACK ? F("READBACK") : F("UNKNOWN"));
      Serial.print(F(" holding="));
      Serial.print(holding ? 1 : 0);
      Serial.print(F(" haveTarget="));
      Serial.println(haveTarget ? 1 : 0);
      break;
    default: break;
  }
  lineLen = 0;
}

void setup() {
  Serial.begin(115200);
  Wire.begin();
  pwm.begin();
  bool freqWasSet = ensurePwmFreq();
  delay(10);
  setup_start_pose();
  // Deliberately no pwm.setPWM here. Writing anything now is exactly the
  // startup jump this firmware exists to prevent.
  Serial.print(F("# avi_servo_v2 ready, start mode "));
  Serial.print(startMode == MODE_READBACK ? F("READBACK") : F("UNKNOWN"));
  Serial.print(F(", pwm freq "));
  Serial.println(freqWasSet ? F("reprogrammed") : F("already correct"));
}

void loop() {
  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\n' || ch == '\r') {
      handleLine();
    } else if (lineLen < sizeof(line) - 1) {
      line[lineLen++] = ch;
    } else {
      lineLen = 0;              // overlong garbage: drop it rather than split it
    }
  }

  static uint32_t lastTick = 0;
  uint32_t now = millis();
  if (now - lastTick < TICK_MS) return;
  lastTick = now;

  if (!haveTarget) return;                       // never move unbidden
  if (holding) return;                           // E
  if (now - lastCmdMs > WATCHDOG_MS) return;     // host went quiet: hold still

  float cap = (homing || startMode == MODE_UNKNOWN)
              ? HOMING_DEG_S : SPEED_CAP_DEG_S;
  float maxStep = cap * (float)TICK_MS / 1000.0f;

  bool moved = false;
  for (uint8_t j = 0; j < NJ; j++) {
    if (!JOINTS[j].driven) continue;
    float err = target[j] - current[j];
    if (err > maxStep)       err = maxStep;
    else if (err < -maxStep) err = -maxStep;
    if (err != 0.0f) {
      current[j] = clampDeg(j, current[j] + err);
      pwm.setPWM(j, 0, degToCount(j, current[j]));
      moved = true;
    }
  }
  if (homing && !moved) homing = false;
}
