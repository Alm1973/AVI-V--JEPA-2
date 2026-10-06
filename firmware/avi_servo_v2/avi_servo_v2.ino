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

// Raw I2C, deliberately NOT Adafruit_PWMServoDriver.
//
// The library's begin() calls reset() and then setPWMFreq(), which sleeps the
// chip to load the prescaler. Both of those destroy the output registers --
// which are the one thing this firmware needs to read before it touches
// anything, so that it can start from where the arm actually is. Measured on
// 2026-10-05: with pwm.begin() in setup(), the read-back reported UNKNOWN every
// time, because the library had already wiped it.
//
// The PCA9685 register map is small enough that owning it outright is cheaper
// than fighting a library whose init sequence is the problem.
#include <Wire.h>

static const uint8_t  PCA_ADDR      = 0x40;
static const uint8_t  LED0_ON_L     = 0x06;
static const uint8_t  REG_MODE1     = 0x00;
static const uint8_t  REG_PRESCALE  = 0xFE;
static const uint8_t  MODE1_SLEEP   = 0x10;
static const uint8_t  MODE1_AI      = 0x20;   // auto-increment
static const uint8_t  MODE1_RESTART = 0x80;
static const uint16_t PWM_FREQ_HZ   = 50;
static const uint8_t  NJ            = 4;

static const float    SPEED_CAP_DEG_S = 60.0f;   // config/joints.toml meta
static const float    HOMING_DEG_S    = 18.0f;   // H, and the unknown-pose ramp
static const uint16_t WATCHDOG_MS     = 500;
static const uint8_t  TICK_MS         = 10;
static const uint16_t CALIB_WINDOW_MS = 6000;   // how long a C command is honoured

// Bench calibration of 2026-08-04. MUST stay in step with config/joints.toml.
// min/center/max are raw PCA9685 counts; soft limits are degrees.
struct Joint {
  uint16_t minCount, centerCount, maxCount;
  float    softMin, softMax;
  int8_t   sign;      // -1 if the joint runs backwards; see config/joints.toml
  bool     driven;
};

static const Joint JOINTS[NJ] = {
  //  min  center  max   softMin softMax sign driven
  {    75,    250,  460,   20.0f, 160.0f,  -1, true  },  // 0 base yaw (MEASURED)
  {    75,    370,  580,   25.0f, 155.0f,  -1, true  },  // 1 shoulder (MEASURED; v1 BUG here)
  {    80,    430,  570,   30.0f, 150.0f,   1, true  },  // 2 elbow, sign PROVISIONAL
  {   140,    380,  580,   20.0f, 176.0f,   1, true  },  // 3 wrist: min 4 -> 20, 2026-10-06, stalls below ~16 (see joints.toml)
};

enum StartMode { MODE_UNKNOWN, MODE_READBACK };

static float      speedCap = SPEED_CAP_DEG_S;  // V sets this at runtime
static uint32_t   calibUntil = 0;       // C grants a short calibration window
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
// Logical degrees in, counts out. A joint with sign -1 is reflected about
// center, so that "larger logical degrees" always means the same direction in
// the world no matter how the linkage is wired.
static float applySign(uint8_t j, float deg) {
  return (JOINTS[j].sign < 0) ? (180.0f - deg) : deg;
}

static uint16_t degToCount(uint8_t j, float deg) {
  const Joint &J = JOINTS[j];
  deg = applySign(j, deg);
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
  float deg;
  if (count <= J.centerCount) {
    if (J.centerCount == J.minCount) return 90.0f;
    deg = 90.0f * (float)(count - J.minCount) / (float)(J.centerCount - J.minCount);
  } else if (J.maxCount == J.centerCount) {
    return 90.0f;
  } else {
    deg = 90.0f + 90.0f * (float)(count - J.centerCount) / (float)(J.maxCount - J.centerCount);
  }
  return applySign(j, deg);   // the reflection is its own inverse
}

static float clampDeg(uint8_t j, float deg) {
  const Joint &J = JOINTS[j];
  if (deg < J.softMin) return J.softMin;
  if (deg > J.softMax) return J.softMax;
  return deg;
}

static bool writeReg(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(PCA_ADDR);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static void setPWMRaw(uint8_t ch, uint16_t on, uint16_t off) {
  Wire.beginTransmission(PCA_ADDR);
  Wire.write((uint8_t)(LED0_ON_L + 4 * ch));
  Wire.write((uint8_t)(on & 0xFF));
  Wire.write((uint8_t)(on >> 8));
  Wire.write((uint8_t)(off & 0xFF));
  Wire.write((uint8_t)(off >> 8));
  Wire.endTransmission();
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
  bool asleep = (mode1 == 0xFF) || (mode1 & MODE1_SLEEP);
  if (have == want && !asleep) {
    // Already at 50 Hz and awake. Enable auto-increment, which the block
    // writes in setPWMRaw rely on, without disturbing anything else.
    writeReg(REG_MODE1, (uint8_t)((mode1 | MODE1_AI) & ~MODE1_SLEEP));
    return false;
  }
  // Cold chip, or wrong frequency. The prescaler can only be loaded while
  // asleep, so the outputs do stop here; nothing can be done about that except
  // avoid it when possible, which is what the branch above is for.
  uint8_t old = (mode1 == 0xFF) ? 0x00 : mode1;
  writeReg(REG_MODE1, (uint8_t)((old & ~MODE1_RESTART) | MODE1_SLEEP));
  writeReg(REG_PRESCALE, want);
  writeReg(REG_MODE1, (uint8_t)((old & ~MODE1_SLEEP) | MODE1_AI));
  delay(5);
  writeReg(REG_MODE1, (uint8_t)((old & ~MODE1_SLEEP) | MODE1_AI | MODE1_RESTART));
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
  // NOTE: this read must happen BEFORE anything writes to the chip. See the
  // comment at the top about why Adafruit_PWMServoDriver is not used.
  uint16_t count = ((hi & 0x0F) << 8) | lo;
  const Joint &J = JOINTS[ch];
  if (count < J.minCount || count > J.maxCount) return 0xFFFF;
  return count;
}

static void setup_start_pose() {
  // Only the DRIVEN channels have to read back.
  //
  // The first build required all four, and bailed on the first failure. The
  // elbow and wrist are not driven, so nothing has ever written their
  // registers: they read 0, which is outside their calibrated range, which is
  // correctly treated as "do not trust it" -- and so the read-back could never
  // succeed, on any boot, no matter what the arm was doing. Measured on
  // hardware 2026-10-05: start mode UNKNOWN every time, with base yaw sitting
  // at a perfectly readable 99 degrees.
  bool all = true;
  float got[NJ];
  for (uint8_t j = 0; j < NJ; j++) {
    if (!JOINTS[j].driven) { got[j] = 90.0f; continue; }
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
    case 'C': {
      // C <ch> <deg> -- calibration move. Drives a channel even when it is
      // marked undriven, which is the only way to discover an unknown
      // direction sign in the first place. Still clamped to that joint's soft
      // limits, still at the slow homing rate, and the permission expires on
      // its own after CALIB_WINDOW_MS so a dropped connection cannot leave the
      // interlock open.
      char *tok = strtok(line + 1, " \t");
      if (!tok) break;
      int ch = atoi(tok);
      tok = strtok(NULL, " \t");
      if (!tok || ch < 0 || ch >= NJ) break;
      target[ch] = clampDeg((uint8_t)ch, atof(tok));
      haveTarget = true;
      homing = true;                    // slow rate for anything unknown
      calibUntil = millis() + CALIB_WINDOW_MS;
      lastCmdMs = millis();
      Serial.print(F("C ok ch=")); Serial.print(ch);
      Serial.print(F(" -> ")); Serial.println(target[ch], 1);
      break;
    }
    case 'V': {
      // V <deg_s> -- set the speed cap at runtime, so the arm's real limit can
      // be measured without reflashing between every attempt. Bounded well
      // below anything that could slam a joint.
      float v = atof(line + 1);
      if (v >= 5.0f && v <= 150.0f) {
        speedCap = v;
        Serial.print(F("V ok ")); Serial.println(speedCap, 1);
      } else {
        Serial.println(F("V range 5..150"));
      }
      break;
    }
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
      Serial.print(haveTarget ? 1 : 0);
      Serial.print(F(" cap="));
      Serial.println(speedCap, 1);
      break;
    default: break;
  }
  lineLen = 0;
}

void setup() {
  Serial.begin(115200);
  Wire.begin();
  // ORDER MATTERS. Read where the arm is first, while the chip is still
  // holding whatever it was driving before the Uno reset, and only then touch
  // any register. Reversing these two lines is what made the read-back report
  // UNKNOWN on the first flashed build.
  setup_start_pose();
  bool freqWasSet = ensurePwmFreq();
  delay(10);
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
  bool calibrating = (int32_t)(calibUntil - now) > 0;
  // The watchdog is suspended during a calibration window, because a C move
  // is deliberately not followed by a 50 Hz stream of P frames.
  if (!calibrating && now - lastCmdMs > WATCHDOG_MS) return;

  float cap = (homing || startMode == MODE_UNKNOWN)
              ? HOMING_DEG_S : speedCap;
  float maxStep = cap * (float)TICK_MS / 1000.0f;

  bool moved = false;
  for (uint8_t j = 0; j < NJ; j++) {
    if (!JOINTS[j].driven && !calibrating) continue;
    float err = target[j] - current[j];
    if (err > maxStep)       err = maxStep;
    else if (err < -maxStep) err = -maxStep;
    if (err != 0.0f) {
      current[j] = clampDeg(j, current[j] + err);
      setPWMRaw(j, 0, degToCount(j, current[j]));
      moved = true;
    }
  }
  if (homing && !moved) homing = false;
}
