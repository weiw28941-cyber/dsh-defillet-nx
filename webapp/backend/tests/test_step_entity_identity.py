from pathlib import Path

from cad.OccUtils.map_freecad_face_to_step import entity_info, enumerate_faces, parse_text_faces, read_step


def test_entity_info_uses_literal_part21_advanced_face_id():
    step_path = Path(__file__).with_name("seed.step")
    reader, shape = read_step(str(step_path))
    faces = enumerate_faces(shape)
    text_faces = parse_text_faces(str(step_path))

    assert len(faces) == len(text_faces)
    actual = [entity_info(reader, face)["step_entity_id"] for face in faces]
    expected = [row["step_entity_id"] for row in text_faces]
    assert actual == expected
