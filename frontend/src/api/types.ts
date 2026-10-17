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

export type Work = {
  id: string;
  title: string;
  cover_path: string;
};

export type Benefit = {
  id: string;
  title: string;
  description: string;
};

export type BenefitFile = {
  id: string;
  position: number;
  title: string;
  mime_type: string;
};

export type LoginResponse = {
  account: Account;
  return_to: string;
  csrf_token: string;
};

export type DemoAccountsResponse = {
  accounts: Account[];
};

export type ReceiveResponse = {
  state: BenefitDisplayState;
  work: Work;
  benefit: Benefit;
  files: BenefitFile[];
};

export type ClaimAccountResponse = {
  benefit_id: string;
};

export type ClaimDownloadResponse = {
  expires_at: string;
  remaining_seconds: number;
};

export type DownloadResponse = {
  expires_at: string;
  remaining_seconds: number;
  work: Work;
  benefit: Benefit;
  files: BenefitFile[];
};

export type LibraryResponse = {
  items: { work: Work; benefit: Benefit }[];
};

export type PrepareLoginResponse = {
  return_to: string;
};

export type LogoutResponse = {
  csrf_token: string;
};

export type LibraryBenefitResponse = {
  work: Work;
  benefit: Benefit;
  files: BenefitFile[];
};
