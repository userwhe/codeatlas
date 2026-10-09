import { SignIn } from "./sign-in";

export default async function HomePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { error } = await searchParams;
  return <SignIn failed={error === "sign_in_failed"} notInvited={error === "not_invited"} />;
}
