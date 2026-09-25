using CommunityToolkit.Mvvm.ComponentModel;

namespace Pdv.App;

/// <summary>
/// O que o balcão vê da sincronização: se a nuvem está ao alcance e quanto
/// falta subir.
/// </summary>
/// <remarks>
/// A fila à vista é o que faz alguém notar um caixa que parou de sincronizar
/// no dia, e não no fechamento do mês. Quem chama <see cref="Update"/> já está
/// na thread da tela: o worker roda fora dela e a casca faz a ponte.
/// </remarks>
public sealed partial class SyncStatusViewModel : ObservableObject
{
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(Text))]
    public partial bool? Online { get; private set; }

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(Text))]
    public partial long Pending { get; private set; }

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(Text))]
    public partial long Quarantined { get; private set; }

    /// <summary>Terminal em demonstração: não há para onde sincronizar.</summary>
    public bool Disabled { get; init; }

    public string Text
    {
        get
        {
            if (Disabled) return "Demonstração: sem sincronização";
            var state = Online switch
            {
                null => "Conectando à retaguarda…",
                true => "Retaguarda: online",
                false => "Retaguarda: sem conexão",
            };
            var queue = Pending == 0 ? "tudo enviado" : $"{Pending} registro(s) a enviar";
            var stuck = Quarantined > 0 ? $" · {Quarantined} recusado(s): chame o suporte" : "";
            return $"{state} · {queue}{stuck}";
        }
    }

    public void Update(bool? online = null, long? pending = null, long? quarantined = null)
    {
        if (online is not null) Online = online;
        if (pending is not null) Pending = pending.Value;
        if (quarantined is not null) Quarantined = quarantined.Value;
    }
}
