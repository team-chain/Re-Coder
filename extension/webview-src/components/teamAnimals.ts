/**
 * 팀 모드 팀원 그림 — 모두 Recoder Character(리코더)로 그린다.
 * AnimalKind 는 예전 저장 상태(창을 다시 불러와도 구성 유지)와의 호환을 위해 **내부 자리 번호**로만 남긴다.
 * 화면에는 동물 이름·동물 그림을 쓰지 않는다. 문자열은 고정 data URI 이미지뿐이라 외부 입력이 섞이지 않는다.
 */
import { characterImg } from "./recoderCharacter";

export type AnimalKind = "dog" | "cat" | "rabbit" | "squirrel" | "penguin" | "panda";
export const ANIMAL_KINDS: AnimalKind[] = ["dog","cat","rabbit","squirrel","penguin","panda"];

/** 얼굴 크기(구성 막대 칩). 종류와 상관없이 리코더. */
export function faceSvg(_kind: AnimalKind, size = 22): string {
  return characterImg(size);
}
/** 몸 전체(팀 보드·배달). 종류와 상관없이 리코더. */
export function bodySvg(_kind: AnimalKind, size = 44): string {
  return characterImg(size);
}

/** 아직 안 나온 동물 중 하나를 무작위로. 6마리를 다 쓰면 다시 섞는다. */
export function pickAnimal(used: AnimalKind[], rand: () => number = Math.random): AnimalKind {
  const left = ANIMAL_KINDS.filter(k => !used.includes(k));
  const pool = left.length ? left : ANIMAL_KINDS;
  return pool[Math.min(pool.length - 1, Math.floor(rand() * pool.length))];
}
