// Leave the Wizard console.
//
// 結束場次 posts /end and closes everyone's live token.
// 離開場次 only forgets this device's join info and stops reconnecting.
// It does not end the session and does not reload the page.

export interface WizardLeavePlan {
  clearSavedJoin: true;
  reload: false;
  endsSession: false;
  buttonLabel: string;
  title: string;
  detail: string;
}

export function planWizardLeave(sessionEnded: boolean): WizardLeavePlan {
  if (sessionEnded) {
    return {
      clearSavedJoin: true,
      reload: false,
      endsSession: false,
      buttonLabel: "離開場次",
      title: "離開場次，回到加入頁",
      detail:
        "場次已經結束。離開會清除這台裝置保存的加入資料、停止重新連線，並回到加入頁。這不是「結束場次」（場次已經結束），也不會替學生按 Stop REC。",
    };
  }
  return {
    clearSavedJoin: true,
    reload: false,
    endsSession: false,
    buttonLabel: "離開場次",
    title: "離開此頁，不結束場次",
    detail:
      "清除這台裝置的加入資料並停止重新連線，回到加入頁。場次會繼續。要結束所有人的場次請按「結束場次」，不要按離開。",
  };
}
