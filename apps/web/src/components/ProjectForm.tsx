import type { FormEvent } from "react";
import Button from "react-bootstrap/Button";
import Col from "react-bootstrap/Col";
import Form from "react-bootstrap/Form";
import Row from "react-bootstrap/Row";
import Stack from "react-bootstrap/Stack";
import type { ProjectFormValues } from "../studio";
import { createMetadataEntry } from "../studio";

type ProjectFormProps = {
  eyebrow: string;
  title: string;
  description: string;
  submitLabel: string;
  value: ProjectFormValues;
  disabled?: boolean;
  onChange: (nextValue: ProjectFormValues) => void;
  onSubmit: () => void;
};

export function ProjectForm({
  eyebrow,
  title,
  description,
  submitLabel,
  value,
  disabled = false,
  onChange,
  onSubmit,
}: ProjectFormProps) {
  function updateField(field: keyof Omit<ProjectFormValues, "metadataEntries">, nextValue: string) {
    onChange({
      ...value,
      [field]: nextValue,
    });
  }

  function updateMetadataEntry(entryId: string, field: "key" | "value", nextValue: string) {
    onChange({
      ...value,
      metadataEntries: value.metadataEntries.map((entry) =>
        entry.id === entryId ? { ...entry, [field]: nextValue } : entry,
      ),
    });
  }

  function addMetadataEntry() {
    onChange({
      ...value,
      metadataEntries: [...value.metadataEntries, createMetadataEntry()],
    });
  }

  function removeMetadataEntry(entryId: string) {
    onChange({
      ...value,
      metadataEntries: value.metadataEntries.filter((entry) => entry.id !== entryId),
    });
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (disabled || !value.name.trim()) {
      return;
    }
    onSubmit();
  }

  return (
    <Form onSubmit={handleSubmit}>
      <Stack gap={3}>
        <div className="studio-panel__header">
          <div>
            <p className="eyebrow">{eyebrow}</p>
            <h3 className="form-panel__title">{title}</h3>
          </div>
          <p className="section-footnote">{description}</p>
        </div>

        <Row className="g-3">
          <Col xs={12}>
            <Form.Group>
              <Form.Label>名前</Form.Label>
              <Form.Control
                type="text"
                value={value.name}
                onChange={(event) => updateField("name", event.target.value)}
                placeholder="春キャンペーン"
                disabled={disabled}
              />
            </Form.Group>
          </Col>

          <Col xs={12}>
            <Form.Group>
              <Form.Label>説明</Form.Label>
              <Form.Control
                as="textarea"
                rows={3}
                value={value.description}
                onChange={(event) => updateField("description", event.target.value)}
                placeholder="KV、音声ループ、絵コンテのまとめ"
                disabled={disabled}
              />
            </Form.Group>
          </Col>

          <Col md={6}>
            <Form.Group>
              <Form.Label>状態</Form.Label>
              <Form.Control
                type="text"
                value={value.status}
                onChange={(event) => updateField("status", event.target.value)}
                placeholder="進行中"
                disabled={disabled}
              />
            </Form.Group>
          </Col>

          <Col md={6}>
            <Form.Group>
              <Form.Label>タグ</Form.Label>
              <Form.Control
                type="text"
                value={value.tagsText}
                onChange={(event) => updateField("tagsText", event.target.value)}
                placeholder="キャンペーン, ローンチ"
                disabled={disabled}
              />
            </Form.Group>
          </Col>
        </Row>

        <div className="editor-block">
          <div className="studio-panel__header">
            <div>
              <p className="eyebrow">メタデータ</p>
              <h3 className="form-panel__title">必要な情報だけを追加する</h3>
            </div>
            <Button
              type="button"
              variant="outline-secondary"
              size="sm"
              onClick={addMetadataEntry}
              disabled={disabled}
            >
              追加する
            </Button>
          </div>

          {value.metadataEntries.length > 0 ? (
            <Stack gap={2}>
              {value.metadataEntries.map((entry) => (
                <Row key={entry.id} className="g-2 align-items-end">
                  <Col md={5}>
                    <Form.Group>
                      <Form.Label>項目名</Form.Label>
                      <Form.Control
                        type="text"
                        value={entry.key}
                        onChange={(event) =>
                          updateMetadataEntry(entry.id, "key", event.target.value)
                        }
                        placeholder="担当者"
                        disabled={disabled}
                      />
                    </Form.Group>
                  </Col>
                  <Col md={5}>
                    <Form.Group>
                      <Form.Label>内容</Form.Label>
                      <Form.Control
                        type="text"
                        value={entry.value}
                        onChange={(event) =>
                          updateMetadataEntry(entry.id, "value", event.target.value)
                        }
                        placeholder="社内案件"
                        disabled={disabled}
                      />
                    </Form.Group>
                  </Col>
                  <Col md={2}>
                    <Button
                      type="button"
                      variant="outline-secondary"
                      className="w-100"
                      onClick={() => removeMetadataEntry(entry.id)}
                      disabled={disabled}
                    >
                      削除
                    </Button>
                  </Col>
                </Row>
              ))}
            </Stack>
          ) : (
            <div className="history-empty">
              必要なときだけ補足情報を追加してください。担当者や案件名の整理に使えます。
            </div>
          )}
        </div>

        <div className="d-flex justify-content-end">
          <Button type="submit" disabled={disabled || !value.name.trim()}>
            {submitLabel}
          </Button>
        </div>
      </Stack>
    </Form>
  );
}
