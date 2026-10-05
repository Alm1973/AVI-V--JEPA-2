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
