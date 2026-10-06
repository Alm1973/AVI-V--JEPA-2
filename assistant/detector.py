"""YOLO-World wrapper that proposes desk objects for the VLM to verify.

Nothing it finds goes into memory until the VLM says yes on the crop
(design rule 3: assume clutter).
"""
import settings


class DeskDetector:
    def __init__(self, vocabulary=settings.DESK_OBJECTS,
                 model_name=settings.DETECTOR_MODEL,
                 conf=settings.DETECTOR_CONF):
        from ultralytics import YOLOWorld
        self.model = YOLOWorld(model_name)
        self.vocabulary = list(vocabulary)
        self.classes = self.vocabulary + ["person"]
        self.model.set_classes(self.classes)
        self.conf = conf

    def detect(self, frame):
        """Returns (candidates, person_conf).

        candidates: list of {label, conf, box=(x1, y1, x2, y2)} for desk objects,
        highest score first. person_conf: best "person" score, 0.0 if none.
        """
        res = self.model.predict(frame, conf=self.conf, verbose=False)[0]
        candidates, person_conf = [], 0.0
        for b in res.boxes:
            label = self.model.names[int(b.cls.item())]
            conf = float(b.conf.item())
            if label == "person":
                person_conf = max(person_conf, conf)
                continue
            x1, y1, x2, y2 = (int(v) for v in b.xyxy[0].tolist())
            candidates.append({"label": label, "conf": round(conf, 3),
                               "box": (x1, y1, x2, y2)})
        candidates.sort(key=lambda c: -c["conf"])
        return candidates, person_conf

    def people(self, frame, min_conf=0.55):
        """Every person box above `min_conf`, highest confidence first.

        person() returns only the winner, which is no use to the parallax
        test: deciding whether a candidate is the arm's own hardware means
        watching THAT candidate across a pan, and a candidate that loses on
        confidence this frame is exactly the one that needs watching.
        """
        # The MODEL's own threshold has to come down too, or min_conf can
        # only ever be raised: asking for 0.10 while predicting at
        # settings.DETECTOR_CONF returns nothing below that and looks exactly
        # like an empty room. Cost a probe run on 2026-10-05.
        res = self.model.predict(frame, conf=min(self.conf, min_conf),
                                 verbose=False)[0]
        out = []
        for b in res.boxes:
            if self.model.names[int(b.cls.item())] != "person":
                continue
            conf = float(b.conf.item())
            if conf < min_conf:
                continue
            x1, y1, x2, y2 = (float(v) for v in b.xyxy[0].tolist())
            out.append({"box": (x1, y1, x2, y2), "conf": conf,
                        "cx": (x1 + x2) / 2.0, "cy": (y1 + y2) / 2.0})
        out.sort(key=lambda d: -d["conf"])
        return out

    def everything(self, frame, min_conf=0.05):
        """Every detection of any class, for diagnosis. Numbers only.

        Deliberately returns no pixels. The camera faces Shaurya's room, so
        the way to answer "what does it actually see" from another machine is
        to ship the labels and leave the frame here.
        """
        res = self.model.predict(frame, conf=min_conf, verbose=False)[0]
        out = []
        for b in res.boxes:
            x1, y1, x2, y2 = (float(v) for v in b.xyxy[0].tolist())
            out.append({"label": self.model.names[int(b.cls.item())],
                        "conf": round(float(b.conf.item()), 3),
                        "cx": round((x1 + x2) / 2.0),
                        "cy": round((y1 + y2) / 2.0),
                        "w": round(x2 - x1), "h": round(y2 - y1)})
        out.sort(key=lambda d: -d["conf"])
        return out

    def person(self, frame, min_conf=0.75, near=None, near_bonus=0.10):
        """Best person in the frame, or None.

        `min_conf` is deliberately far above the model threshold, and 0.75
        rather than 0.55 for a measured reason. Tracking on 2026-10-05 found
        Shaurya at 0.81-0.89 and a spurious box at 0.28-0.48; a floor of 0.55
        let a THIRD thing through at 0.56-0.72, and that one never moved in the
        image however far the arm panned -- base_yaw swung from 40 to 57
        degrees while its x stayed at 1035, where a real person would have
        shifted 360 px. Something that does not move when the camera pans is
        rigidly attached to the camera: it was the arm's own hardware in shot.
        The loop then had an error it could never reduce, so it saturated,
        pointed away from him, lost him, and locked onto itself.

        His real detections sit comfortably above 0.75, so that is where the
        floor goes. A missed frame costs nothing -- the gaze is sticky for two
        seconds and relaxes to centre after that, which is the right behaviour
        when it cannot see him.

        `near` is the previous accepted x, and a detection close to it gets a
        small bonus. A person does not teleport, so continuity is real
        evidence, and without it two similarly-confident boxes make the gaze
        flick between them.
        """
        """Returns ({box, conf, cx, cy}, ...) for the best person, or None.

        detect() reports only a person's CONFIDENCE and drops the box, which is
        all the logger needs. look_at needs the box: it steers from the box's
        offset from image centre, so the geometry is the whole point. Kept as a
        separate method rather than widening detect()'s return, so nothing that
        already unpacks two values has to change.
        """
        res = self.model.predict(frame, conf=self.conf, verbose=False)[0]
        width = frame.shape[1]
        best = None
        best_score = 0.0
        for b in res.boxes:
            if self.model.names[int(b.cls.item())] != "person":
                continue
            conf = float(b.conf.item())
            if conf < min_conf:
                continue
            x1, y1, x2, y2 = (float(v) for v in b.xyxy[0].tolist())
            cx = (x1 + x2) / 2.0
            score = conf
            if near is not None and width:
                closeness = 1.0 - min(1.0, abs(cx - near) / (width / 2.0))
                score += near_bonus * closeness
            if best is not None and score <= best_score:
                continue
            best_score = score
            best = {"box": (x1, y1, x2, y2), "conf": conf,
                    "cx": cx, "cy": (y1 + y2) / 2.0}
        return best
