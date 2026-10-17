export type ApiErrorBody = {
  error: {
    code: string;
    message: string;
  };
};

export type Account = {
  id: string;
  name: string;
};

export type SessionResponse = {
  account: Account | null;
  csrf_token: string;
};

export type BenefitDisplayState =
  | 'unused'
  | 'account_owned'
  | 'download_retryable'
  | 'claimed';

// 仮：表示情報のフィールドはバックエンドのAPI契約確定時に合わせる
export type Work = {
  id: string;
  title: string;
  cover_path: string;
};

// 仮
export type Benefit = {
  id: string;
  title: string;
  description: string;
};

// 仮：サーバー上のファイルパスは含めない
export type BenefitFile = {
  id: string;
  position: number;
  title: string;
  mime_type: string;
};

// 仮
export type LoginResponse = {
  account: Account;
  return_to: string;
  csrf_token: string; // 仮
};

// 仮
export type DemoAccountsResponse = {
  accounts: Account[];
};

// 仮
export type ReceiveResponse = {
  state: BenefitDisplayState;
  work: Work;
  benefit: Benefit;
  files: BenefitFile[];
};

// 仮
export type ClaimAccountResponse = {
  benefit_id: string;
};

// 仮
export type ClaimDownloadResponse = {
  expires_at: string;
};

// 仮
export type DownloadResponse = {
  expires_at: string;
  remaining_seconds: number; // 仮
  work: Work;
  benefit: Benefit;
  files: BenefitFile[];
};

// 仮
export type LibraryResponse = {
  items: { work: Work; benefit: Benefit }[];
};

// 仮
export type LibraryBenefitResponse = {
  work: Work;
  benefit: Benefit;
  files: BenefitFile[];
};
