import type { Benefit, BenefitFile, Work } from './types';

// APIの成功応答の実行時検証に使う
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

export function isWork(value: unknown): value is Work {
  return isRecord(value) &&
    typeof value.id === 'string' &&
    typeof value.title === 'string' &&
    typeof value.cover_path === 'string';
}

export function isBenefit(value: unknown): value is Benefit {
  return isRecord(value) &&
    typeof value.id === 'string' &&
    typeof value.title === 'string' &&
    typeof value.description === 'string';
}

export function isBenefitFiles(value: unknown): value is BenefitFile[] {
  return Array.isArray(value) && value.every((file: unknown) => isRecord(file) &&
    typeof file.id === 'string' &&
    typeof file.position === 'number' &&
    typeof file.title === 'string' &&
    typeof file.mime_type === 'string');
}
